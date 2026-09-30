// Package docker implements driver.Driver on the Docker Engine API.
package docker

import (
	"archive/tar"
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/moby/moby/api/pkg/stdcopy"
	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
	"github.com/moby/moby/client"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// Driver talks to one docker daemon.
type Driver struct {
	cli *client.Client
}

var _ driver.Driver = (*Driver)(nil)

// New connects with the standard DOCKER_HOST / DOCKER_* environment and checks the daemon
// answers.
func New(ctx context.Context) (*Driver, error) {
	cli, err := client.New(client.FromEnv)
	if err != nil {
		return nil, fmt.Errorf("docker client from environment: %w", err)
	}
	if _, err := cli.Ping(ctx, client.PingOptions{NegotiateAPIVersion: true}); err != nil {
		return nil, fmt.Errorf("docker daemon at %s does not answer. Is it running, and is DOCKER_HOST right? %w", cli.DaemonHost(), err)
	}
	return &Driver{cli: cli}, nil
}

// Close releases the client's connections.
func (d *Driver) Close() error { return d.cli.Close() }

func (d *Driver) Runtimes(ctx context.Context) ([]string, error) {
	info, err := d.cli.Info(ctx, client.InfoOptions{})
	if err != nil {
		return nil, fmt.Errorf("docker info: %w", err)
	}
	var out []string
	for name := range info.Info.Runtimes {
		out = append(out, name)
	}
	slices.Sort(out)
	return out, nil
}

func (d *Driver) ExportImagePath(ctx context.Context, image, path string, labels map[string]string) (io.ReadCloser, error) {
	created, err := d.cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Config:     &container.Config{Image: image, Labels: labels, Entrypoint: []string{"true"}},
		HostConfig: &container.HostConfig{NetworkMode: "none"},
	})
	if err != nil {
		return nil, imageError(image, err)
	}
	id := driver.ContainerID(created.ID)
	copied, err := d.cli.CopyFromContainer(ctx, created.ID, client.CopyFromContainerOptions{SourcePath: path})
	if err != nil {
		removeErr := d.Remove(context.WithoutCancel(ctx), id)
		if cerrdefs.IsNotFound(err) {
			return nil, errors.Join(driver.ErrNotFound, removeErr)
		}
		return nil, errors.Join(fmt.Errorf("copy %s out of image %s: %w", path, image, err), removeErr)
	}
	return &removeOnClose{ReadCloser: copied.Content, remove: func() error {
		return d.Remove(context.WithoutCancel(ctx), id)
	}}, nil
}

type removeOnClose struct {
	io.ReadCloser
	remove func() error
}

func (r *removeOnClose) Close() error {
	return errors.Join(r.ReadCloser.Close(), r.remove())
}

// CreateContainer starts `sleep infinity` under docker-init, offline, with every capability
// dropped. The image must provide `sleep`, and `/bin/sh` for exec timeouts.
func (d *Driver) CreateContainer(ctx context.Context, spec driver.ContainerSpec) (driver.ContainerID, error) {
	init := true
	host := &container.HostConfig{
		NetworkMode: "none",
		CapDrop:     []string{"ALL"},
		SecurityOpt: []string{"no-new-privileges"},
		Init:        &init,
		Runtime:     spec.Runtime,
		Resources: container.Resources{
			NanoCPUs: spec.Resources.NanoCPUs,
			Memory:   spec.Resources.MemoryBytes,
		},
	}
	if spec.Resources.MemoryBytes > 0 {
		host.MemorySwap = spec.Resources.MemoryBytes
	}
	if spec.Resources.Pids > 0 {
		host.PidsLimit = &spec.Resources.Pids
	}
	if spec.Resources.DiskBytes > 0 {
		host.StorageOpt = map[string]string{"size": strconv.FormatInt(spec.Resources.DiskBytes, 10)}
	}
	for _, b := range spec.Binds {
		host.Mounts = append(host.Mounts, mount.Mount{
			Type:        mount.TypeBind,
			Source:      b.HostPath,
			Target:      b.ContainerPath,
			ReadOnly:    b.ReadOnly,
			BindOptions: &mount.BindOptions{Propagation: mount.PropagationRPrivate},
		})
	}
	created, err := d.cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Config: &container.Config{
			Image:      spec.Image,
			Labels:     spec.Labels,
			Entrypoint: []string{"sleep"},
			Cmd:        []string{"infinity"},
		},
		HostConfig: host,
	})
	if err != nil {
		return "", imageError(spec.Image, err)
	}
	id := driver.ContainerID(created.ID)
	if _, err := d.cli.ContainerStart(ctx, created.ID, client.ContainerStartOptions{}); err != nil {
		return "", errors.Join(fmt.Errorf("start container %s (image %s): %w", created.ID, spec.Image, err),
			d.Remove(context.WithoutCancel(ctx), id))
	}
	return id, nil
}

func imageError(image string, err error) error {
	if cerrdefs.IsNotFound(err) {
		return fmt.Errorf("image %s is not on the docker host. sandboxd does not pull images; pull it there first: %w", image, err)
	}
	return fmt.Errorf("create container from image %s: %w", image, err)
}

func (d *Driver) Exec(ctx context.Context, id driver.ContainerID, spec driver.ExecSpec, stdout, stderr io.Writer) (int, error) {
	created, err := d.cli.ExecCreate(ctx, string(id), client.ExecCreateOptions{
		User:         spec.User,
		WorkingDir:   spec.Cwd,
		Cmd:          spec.Argv,
		AttachStdout: true,
		AttachStderr: true,
	})
	if err != nil {
		return 0, fmt.Errorf("exec create in %s: %w", id, err)
	}
	attached, err := d.cli.ExecAttach(ctx, created.ID, client.ExecAttachOptions{})
	if err != nil {
		return 0, fmt.Errorf("exec start in %s: %w", id, err)
	}
	defer attached.Close()

	copied := make(chan error, 1)
	go func() {
		_, err := stdcopy.StdCopy(stdout, stderr, attached.Reader)
		copied <- err
	}()
	select {
	case err := <-copied:
		if err != nil {
			return 0, fmt.Errorf("read exec output in %s: %w", id, err)
		}
	case <-ctx.Done():
		attached.Close()
		<-copied
		return 0, ctx.Err()
	}

	// The output stream can end a moment before the daemon records the exit code.
	for {
		inspected, err := d.cli.ExecInspect(ctx, created.ID, client.ExecInspectOptions{})
		if err != nil {
			return 0, fmt.Errorf("exec inspect in %s: %w", id, err)
		}
		if !inspected.Running {
			return inspected.ExitCode, nil
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// Processes uses `docker top`, which lists processes from the host side.
func (d *Driver) Processes(ctx context.Context, id driver.ContainerID) ([]driver.Process, error) {
	top, err := d.cli.ContainerTop(ctx, string(id), client.ContainerTopOptions{
		Arguments: []string{"-eo", "pid,ppid,user,args"},
	})
	if err != nil {
		return nil, fmt.Errorf("docker top %s: %w", id, err)
	}
	return parseTop(top.Titles, top.Processes)
}

// parseTop finds columns by title, because runsc's `top` ignores the ps arguments.
func parseTop(titles []string, rows [][]string) ([]driver.Process, error) {
	col := func(names ...string) int {
		for i, t := range titles {
			if slices.Contains(names, t) {
				return i
			}
		}
		return -1
	}
	pid, ppid, user, cmd := col("PID"), col("PPID"), col("USER", "UID"), col("COMMAND", "CMD")
	if pid < 0 || ppid < 0 || user < 0 || cmd < 0 {
		return nil, fmt.Errorf("docker top returned columns %v; need PID, PPID, USER or UID, and COMMAND or CMD", titles)
	}
	out := make([]driver.Process, 0, len(rows))
	for _, row := range rows {
		if len(row) != len(titles) {
			return nil, fmt.Errorf("docker top row %v does not match columns %v", row, titles)
		}
		p, err := strconv.ParseInt(row[pid], 10, 32)
		if err != nil {
			return nil, fmt.Errorf("docker top pid %q: %w", row[pid], err)
		}
		pp, err := strconv.ParseInt(row[ppid], 10, 32)
		if err != nil {
			return nil, fmt.Errorf("docker top ppid %q: %w", row[ppid], err)
		}
		out = append(out, driver.Process{PID: int32(p), PPID: int32(pp), User: row[user], Cmdline: row[cmd]})
	}
	return out, nil
}

func (d *Driver) ReadFile(ctx context.Context, id driver.ContainerID, path string, maxBytes int64) ([]byte, int64, error) {
	copied, err := d.cli.CopyFromContainer(ctx, string(id), client.CopyFromContainerOptions{SourcePath: path})
	if cerrdefs.IsNotFound(err) {
		return nil, 0, driver.ErrNotFound
	}
	if err != nil {
		return nil, 0, fmt.Errorf("copy %s out of %s: %w", path, id, err)
	}
	defer func() { _ = copied.Content.Close() }() // read-only: a close error carries nothing
	tr := tar.NewReader(copied.Content)
	header, err := tr.Next()
	if err != nil {
		return nil, 0, fmt.Errorf("read %s from %s: %w", path, id, err)
	}
	if header.Typeflag != tar.TypeReg {
		return nil, 0, fmt.Errorf("%s in %s is not a regular file", path, id)
	}
	content, err := io.ReadAll(io.LimitReader(tr, maxBytes))
	if err != nil {
		return nil, 0, fmt.Errorf("read %s from %s: %w", path, id, err)
	}
	return content, header.Size, nil
}

func (d *Driver) ListByLabels(ctx context.Context, labels map[string]string) ([]driver.ContainerID, error) {
	filters := client.Filters{}
	for k, v := range labels {
		filters.Add("label", k+"="+v)
	}
	listed, err := d.cli.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: filters})
	if err != nil {
		return nil, fmt.Errorf("list containers labeled %v: %w", labels, err)
	}
	out := make([]driver.ContainerID, len(listed.Items))
	for i, c := range listed.Items {
		out[i] = driver.ContainerID(c.ID)
	}
	return out, nil
}

func (d *Driver) Remove(ctx context.Context, id driver.ContainerID) error {
	_, err := d.cli.ContainerRemove(ctx, string(id), client.ContainerRemoveOptions{Force: true, RemoveVolumes: true})
	if err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("remove container %s: %w", id, err)
	}
	return nil
}

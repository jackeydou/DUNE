// Package docker implements driver.Driver on the Docker Engine API.
package docker

import (
	"archive/tar"
	"bytes"
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
	"github.com/moby/moby/api/types/network"
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

// CreateContainer starts `sleep infinity` under docker-init with every capability dropped. The
// image must provide `sleep`, and `/bin/sh` for exec timeouts.
func (d *Driver) CreateContainer(ctx context.Context, spec driver.ContainerSpec) (driver.ContainerID, error) {
	init := true
	netMode := container.NetworkMode("none")
	var netConfig *network.NetworkingConfig
	if spec.Network != "" {
		netMode = container.NetworkMode(spec.Network)
		netConfig = &network.NetworkingConfig{EndpointsConfig: map[string]*network.EndpointSettings{spec.Network: {}}}
	}
	host := &container.HostConfig{
		NetworkMode: netMode,
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
		HostConfig:       host,
		NetworkingConfig: netConfig,
	})
	if err != nil {
		return "", imageError(spec.Image, err)
	}
	id := driver.ContainerID(created.ID)
	if len(spec.Files) > 0 {
		if err := d.writeFiles(ctx, created.ID, spec.Files); err != nil {
			return "", errors.Join(err, d.Remove(context.WithoutCancel(ctx), id))
		}
	}
	if _, err := d.cli.ContainerStart(ctx, created.ID, client.ContainerStartOptions{}); err != nil {
		return "", errors.Join(fmt.Errorf("start container %s (image %s): %w", created.ID, spec.Image, err),
			d.Remove(context.WithoutCancel(ctx), id))
	}
	return id, nil
}

// writeFiles copies files into a created container. Under runsc the files must be there before
// start: gVisor overlays the root filesystem and does not see later host-side writes.
func (d *Driver) writeFiles(ctx context.Context, id string, files []driver.File) error {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, f := range files {
		h := &tar.Header{Name: f.Path[1:], Mode: int64(f.Mode.Perm()), Uid: f.UID, Gid: f.GID, Typeflag: tar.TypeReg, Size: int64(len(f.Content))}
		if f.Mode.IsDir() {
			h.Name, h.Typeflag, h.Size = h.Name+"/", tar.TypeDir, 0
		}
		if err := tw.WriteHeader(h); err != nil {
			return fmt.Errorf("archive %s for container %s: %w", f.Path, id, err)
		}
		if !f.Mode.IsDir() {
			if _, err := tw.Write(f.Content); err != nil {
				return fmt.Errorf("archive %s for container %s: %w", f.Path, id, err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		return fmt.Errorf("archive files for container %s: %w", id, err)
	}
	_, err := d.cli.CopyToContainer(ctx, id, client.CopyToContainerOptions{DestinationPath: "/", Content: &buf, CopyUIDGID: true})
	if err != nil {
		return fmt.Errorf("copy files into container %s: %w", id, err)
	}
	return nil
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

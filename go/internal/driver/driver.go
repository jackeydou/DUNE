// Package driver is the boundary between sandboxd and a container backend. Everything that
// differs between single machine (docker) and k8s sits behind Driver.
package driver

import (
	"context"
	"errors"
	"io"
)

// Labels every resource sandboxd creates carries. sandboxd never touches a resource without
// LabelManaged.
const (
	LabelManaged   = "swarmeval.managed"
	LabelRunID     = "swarmeval.run_id"
	LabelSandboxID = "swarmeval.sandbox_id"
)

// ErrNotFound is returned when the image path, container, or file asked for does not exist.
var ErrNotFound = errors.New("not found")

// ContainerID is the backend's id for a container.
type ContainerID string

// Bind maps a host directory into the container.
type Bind struct {
	HostPath      string
	ContainerPath string
	ReadOnly      bool
}

// Resources are per-container limits. Zero means no limit.
type Resources struct {
	NanoCPUs    int64
	MemoryBytes int64
	Pids        int64
	DiskBytes   int64
}

// ContainerSpec describes a sandbox container. The driver adds no network: sandboxes are
// offline until net-gateway exists (M1).
type ContainerSpec struct {
	Image     string
	Runtime   string
	Binds     []Bind
	Resources Resources
	Labels    map[string]string
}

// ExecSpec is one command inside a container.
type ExecSpec struct {
	Argv []string
	Cwd  string
	User string
}

// Process is one row of the container's process list.
type Process struct {
	PID     int32
	PPID    int32
	User    string
	Cmdline string
}

// Driver is implemented by each backend. Calls must be safe for concurrent use.
type Driver interface {
	// Runtimes lists the OCI runtimes the backend offers, for example "runc" and "runsc".
	Runtimes(ctx context.Context) ([]string, error)

	// ExportImagePath returns a tar stream of path as it exists in image, rooted at the
	// path's base name. ErrNotFound when the image has no such path. Any temporary resource
	// it needs carries labels and is gone once the stream is closed.
	ExportImagePath(ctx context.Context, image, path string, labels map[string]string) (io.ReadCloser, error)

	// CreateContainer creates and starts a container that stays up until removed.
	CreateContainer(ctx context.Context, spec ContainerSpec) (ContainerID, error)

	// Exec runs a command and copies its output to stdout and stderr until it exits or ctx
	// ends. When ctx ends first it returns ctx's error and leaves the command running.
	Exec(ctx context.Context, id ContainerID, spec ExecSpec, stdout, stderr io.Writer) (exitCode int, err error)

	// Processes lists the container's processes as seen from outside it.
	Processes(ctx context.Context, id ContainerID) ([]Process, error)

	// ReadFile returns up to maxBytes of a file and its full size, without running anything
	// in the container. ErrNotFound when it does not exist.
	ReadFile(ctx context.Context, id ContainerID, path string, maxBytes int64) (content []byte, size int64, err error)

	// ListByLabels returns the containers carrying every label given.
	ListByLabels(ctx context.Context, labels map[string]string) ([]ContainerID, error)

	// Remove force-removes a container. Removing one that is already gone is not an error.
	Remove(ctx context.Context, id ContainerID) error
}

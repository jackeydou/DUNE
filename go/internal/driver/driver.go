// Package driver is the boundary between sandboxd and a container backend. Everything that
// differs between single machine (docker) and k8s sits behind Driver.
package driver

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"net/netip"
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

// ContainerSpec describes a sandbox container.
type ContainerSpec struct {
	Image     string
	Runtime   string
	Binds     []Bind
	Resources Resources
	Labels    map[string]string
	// Env is NAME=value for every process in the container, exec'd commands included.
	Env []string
	// Hostname is the container's hostname. Empty leaves the backend's default.
	Hostname string
	// Network is the name of the network the container joins. Empty means no network at all.
	Network string
	// DNS is the only upstream of docker's embedded resolver, which listens inside every
	// container on a user-defined network under runc. Without it the resolver forwards to the
	// host's resolvers, past the network's gateway.
	DNS netip.Addr
	// Files are written into the container's own filesystem after it is created and before it
	// starts, so the runtime and the backend's user lookup both see them.
	Files []File
}

// File is one entry written into a container before it starts. Parent directories must
// exist in the image or come earlier in the list.
type File struct {
	// Absolute path inside the container.
	Path string
	// Ignored for a directory.
	Content []byte
	// Permission bits, plus fs.ModeDir for a directory.
	Mode fs.FileMode
	UID  int
	GID  int
}

// NetworkID is the backend's id for a network.
type NetworkID string

// NetworkSpec describes a sandbox network: a bridge on which the host has no address, so
// nothing on it can reach the host. Gateway is reserved and assigned to no container.
type NetworkSpec struct {
	Name    string
	Subnet  netip.Prefix
	Gateway netip.Addr
	Labels  map[string]string
}

// ExecSpec is one command inside a container.
type ExecSpec struct {
	Argv []string
	Cwd  string
	User string
}

// Process is one process in a container, with pids as the container sees them.
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

	// Subnets lists the IPv4 subnets of every network the backend has, managed or not.
	Subnets(ctx context.Context) ([]netip.Prefix, error)

	// CreateNetwork creates a sandbox network.
	CreateNetwork(ctx context.Context, spec NetworkSpec) (NetworkID, error)

	// ListNetworksByLabels returns the networks carrying every label given.
	ListNetworksByLabels(ctx context.Context, labels map[string]string) ([]NetworkID, error)

	// RemoveNetwork removes a network no container is attached to. Removing one that is
	// already gone is not an error.
	RemoveNetwork(ctx context.Context, id NetworkID) error

	// CreateContainer creates a container, writes spec.Files into it, and starts it. It
	// stays up until removed.
	CreateContainer(ctx context.Context, spec ContainerSpec) (ContainerID, error)

	// Exec runs a command and copies its output to stdout and stderr until it exits or ctx
	// ends. When ctx ends first it returns ctx's error and leaves the command running.
	Exec(ctx context.Context, id ContainerID, spec ExecSpec, stdout, stderr io.Writer) (exitCode int, err error)

	// ReadFile returns up to maxBytes of a file and its full size, without running anything
	// in the container. ErrNotFound when it does not exist.
	ReadFile(ctx context.Context, id ContainerID, path string, maxBytes int64) (content []byte, size int64, err error)

	// ListByLabels returns the containers carrying every label given.
	ListByLabels(ctx context.Context, labels map[string]string) ([]ContainerID, error)

	// Remove force-removes a container. Removing one that is already gone is not an error.
	Remove(ctx context.Context, id ContainerID) error
}

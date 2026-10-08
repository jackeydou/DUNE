// Package sandboxd creates sandboxes, runs tool calls in them, and reports what each call
// changed on disk and left running. The gRPC surface is in server.go; this file holds the
// behavior. Design: docs/services/sandboxd.md.
package sandboxd

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"net/netip"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

// Error kinds the gRPC layer maps to status codes.
var (
	ErrInvalid  = errors.New("invalid argument")
	ErrNotFound = errors.New("not found")
	ErrExists   = errors.New("already exists")
)

var (
	runIDPattern     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	sandboxIDPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
)

// Config is sandboxd's deployment configuration.
type Config struct {
	// StateDir holds key paths as <StateDir>/<run_id>/<sandbox_id>/fs/<path>. The docker
	// daemon must see it at the same path. Scratch: lost with the host.
	StateDir string
	// Runtime is "runc", "runsc", or "auto" (runsc when the backend offers it).
	Runtime string
	// Network is how sandboxes are networked.
	Network NetworkMode
	// SubnetPool is where sandbox networks get their subnets, SubnetBits long each. Used only
	// with NetworkPerSandbox.
	SubnetPool netip.Prefix
	SubnetBits int
	// InlineLimit caps stdout and stderr in the exec header.
	InlineLimit int
	// OutputLimit caps the stdout or stderr kept at all.
	OutputLimit int
	// ContentLimit caps a changed file whose content is sent back, and ReadFile's default.
	ContentLimit int64
	// ContentBudget caps the file contents sent back for one Exec or FinalDiff.
	ContentBudget int64
	// HelperTimeout bounds each command sandboxd runs in a sandbox for itself: killing a
	// timed-out call, and listing processes.
	HelperTimeout time.Duration
	// SeedLimit caps the total content of a CreateSandbox's seed files.
	SeedLimit int
	// RestoreLimit caps the file content of one RestoreFiles request, under gRPC's 4 MiB
	// message limit.
	RestoreLimit int
	// ProcessListLimit caps a sandbox's process listing. A sandbox controls how many processes
	// it has and how long their command lines are.
	ProcessListLimit int
	// CollectLimit caps each file an Exec collects. A larger file is removed but not sent.
	CollectLimit int64
	// DisplayStartTimeout bounds starting a sandbox's display stack.
	DisplayStartTimeout time.Duration
}

// DefaultConfig returns the limits sandboxd uses unless flags override them.
func DefaultConfig(stateDir string) Config {
	return Config{
		StateDir:         stateDir,
		Runtime:          "auto",
		Network:          NetworkNone,
		SubnetPool:       netip.MustParsePrefix("10.231.0.0/16"),
		SubnetBits:       28,
		InlineLimit:      64 << 10,
		OutputLimit:      16 << 20,
		ContentLimit:     1 << 20,
		ContentBudget:    64 << 20,
		HelperTimeout:    10 * time.Second,
		SeedLimit:        1 << 20,
		RestoreLimit:     3 << 20,
		ProcessListLimit: 4 << 20,
		CollectLimit:     8 << 20,

		DisplayStartTimeout: defaultDisplayStartTimeout,
	}
}

// Service holds every sandbox this sandboxd created. State is in memory only: after a
// restart, sandboxes of earlier runs are unknown until takeover (M3) adopts them.
type Service struct {
	cfg Config
	drv driver.Driver
	log *slog.Logger

	mu        sync.Mutex
	sandboxes map[key]*sandbox
	runs      map[string]*run
	// netMu serializes subnet choice and network creation.
	netMu sync.Mutex
}

type key struct{ run, sandbox string }

type sandbox struct {
	// mu serializes calls, so one call window's changes belong to one caller.
	mu        sync.Mutex
	container driver.ContainerID
	mounts    []Mount
	roots     []fsdiff.Root
	manifest  fsdiff.Manifest
	// live maps the host pid of every process a call left running to that call.
	live map[int32]string
	// execd is set by the first Exec; RestoreFiles is refused after it.
	execd bool
	// displayUID is the uid running the display stack, empty without a display. Its
	// processes are left out of every listing.
	displayUID string
}

// New returns a Service. cfg.StateDir must exist.
func New(cfg Config, drv driver.Driver, log *slog.Logger) *Service {
	return &Service{cfg: cfg, drv: drv, log: log, sandboxes: map[key]*sandbox{}, runs: map[string]*run{}}
}

// Mount is a key path of a sandbox.
type Mount struct {
	Path      string
	ReadOnly  bool
	Protected bool
}

// CreateRequest describes one sandbox.
type CreateRequest struct {
	RunID     string
	SandboxID string
	Image     string
	Mounts    []Mount
	Resources driver.Resources
	Files     []SeedFile
	// Users are added to the image before the sandbox starts.
	Users []string
	// Env, Hostname, and MachineID are the sandbox's identity; see identity.go.
	Env       map[string]string
	Hostname  string
	MachineID string
	// Display, when set, starts the image's display stack before the first manifest.
	Display *Display
}

// SeedFile is written into a key path before the first manifest.
type SeedFile struct {
	Path    string
	Content []byte
	Mode    fs.FileMode
}

// CreateSandbox creates and starts a sandbox, on the network CreateRun made for it if there is
// one, and takes its first manifest. It returns the runtime the sandbox got.
func (s *Service) CreateSandbox(ctx context.Context, req CreateRequest) (string, error) {
	if err := validateIDs(req.RunID, req.SandboxID); err != nil {
		return "", err
	}
	if req.Image == "" {
		return "", fmt.Errorf("%w: sandbox %s of run %s has no image", ErrInvalid, req.SandboxID, req.RunID)
	}
	mounts, err := cleanMounts(req.Mounts)
	if err != nil {
		return "", fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	if err := checkSeeds(req.Files, mounts, s.cfg.SeedLimit); err != nil {
		return "", fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	if err := checkUsers(req.Users); err != nil {
		return "", fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	if err := checkIdentity(req, mounts); err != nil {
		return "", fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	if err := checkDisplay(req.Display, mounts, req.Users); err != nil {
		return "", fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	net, err := s.network(req.RunID, req.SandboxID)
	if err != nil {
		return "", err
	}
	runtime, err := s.runtime(ctx)
	if err != nil {
		return "", err
	}

	k := key{req.RunID, req.SandboxID}
	sb := &sandbox{mounts: mounts, live: map[int32]string{}}
	sb.mu.Lock()
	defer sb.mu.Unlock()
	s.mu.Lock()
	if _, ok := s.sandboxes[k]; ok {
		s.mu.Unlock()
		return "", fmt.Errorf("%w: sandbox %s of run %s", ErrExists, req.SandboxID, req.RunID)
	}
	s.sandboxes[k] = sb
	s.mu.Unlock()

	if err := s.create(ctx, req, sb, runtime, net); err != nil {
		s.mu.Lock()
		delete(s.sandboxes, k)
		s.mu.Unlock()
		return "", err
	}
	return runtime, nil
}

func (s *Service) create(ctx context.Context, req CreateRequest, sb *sandbox, runtime string, net runNetwork) error {
	labels := map[string]string{
		driver.LabelManaged:   "true",
		driver.LabelRunID:     req.RunID,
		driver.LabelSandboxID: req.SandboxID,
	}
	fsDir := filepath.Join(s.cfg.StateDir, req.RunID, req.SandboxID, "fs")
	var binds []driver.Bind
	for _, m := range sb.mounts {
		host := filepath.Join(fsDir, filepath.FromSlash(m.Path))
		if parentMount(sb.mounts, m.Path) == "" {
			if err := os.MkdirAll(host, 0o755); err != nil {
				return fmt.Errorf("create key path %s for sandbox %s: %w", m.Path, req.SandboxID, err)
			}
			if err := s.populate(ctx, req.Image, m.Path, host, labels); err != nil {
				return err
			}
			sb.roots = append(sb.roots, fsdiff.Root{HostDir: host, ContainerPath: m.Path})
		} else if err := os.MkdirAll(host, 0o755); err != nil {
			return fmt.Errorf("create key path %s for sandbox %s: %w", m.Path, req.SandboxID, err)
		}
		binds = append(binds, driver.Bind{HostPath: host, ContainerPath: m.Path, ReadOnly: m.ReadOnly})
	}
	if err := seed(sb.mounts, fsDir, req.Files); err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	if net.name != "" {
		// Outside fs/, so it is not a key path and never diffed. Under runc docker's embedded
		// resolver still listens on 127.0.0.11 (runtime spec Q14); DNS below makes the gateway
		// its only upstream.
		resolv := filepath.Join(s.cfg.StateDir, req.RunID, req.SandboxID, "resolv.conf")
		if err := os.MkdirAll(filepath.Dir(resolv), 0o755); err != nil {
			return fmt.Errorf("create state dir for sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
		}
		if err := os.WriteFile(resolv, []byte("nameserver "+net.gateway.String()+"\n"), 0o644); err != nil {
			return fmt.Errorf("write resolv.conf for sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
		}
		binds = append(binds, driver.Bind{HostPath: resolv, ContainerPath: "/etc/resolv.conf", ReadOnly: true})
	}
	files, err := s.userFiles(ctx, req.Image, req.Users, labels)
	if err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}

	id, err := s.drv.CreateContainer(ctx, driver.ContainerSpec{
		Image:     req.Image,
		Runtime:   runtime,
		Binds:     binds,
		Resources: req.Resources,
		Labels:    labels,
		Env:       envList(req.Env),
		Hostname:  req.Hostname,
		Network:   net.name,
		DNS:       net.gateway,
		Files:     append(files, machineIDFile(req.MachineID)...),
	})
	if err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	sb.container = id
	if req.Display != nil {
		uid, err := s.startDisplay(ctx, sb, req.Display)
		if err != nil {
			return errors.Join(fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err), s.drv.Remove(context.WithoutCancel(ctx), id))
		}
		sb.displayUID = uid
	}
	manifest, err := fsdiff.Walk(sb.roots, nil)
	if err != nil {
		return errors.Join(err, s.drv.Remove(context.WithoutCancel(ctx), id))
	}
	sb.manifest = manifest
	return nil
}

// populate copies the image's content at a key path into its host directory, as docker
// does for a new named volume. A bind mount would otherwise hide it.
func (s *Service) populate(ctx context.Context, image, containerPath, host string, labels map[string]string) error {
	archive, err := s.drv.ExportImagePath(ctx, image, containerPath, labels)
	if errors.Is(err, driver.ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	extractErr := extract(archive, host)
	if err := errors.Join(extractErr, archive.Close()); err != nil {
		return fmt.Errorf("copy %s from image %s into %s: %w", containerPath, image, host, err)
	}
	return nil
}

func (s *Service) runtime(ctx context.Context) (string, error) {
	if s.cfg.Runtime == "runc" {
		return "runc", nil
	}
	offered, err := s.drv.Runtimes(ctx)
	if err != nil {
		return "", err
	}
	if slices.Contains(offered, "runsc") {
		return "runsc", nil
	}
	if s.cfg.Runtime == "runsc" {
		return "", fmt.Errorf("sandboxd is configured for runtime runsc, but the backend offers only %v. Install gVisor, or start sandboxd with --runtime auto", offered)
	}
	return "runc", nil
}

// SandboxChanges are one sandbox's changes found by FinalDiff.
type SandboxChanges struct {
	SandboxID string
	Changes   []Change
}

// FinalDiff diffs every sandbox of a run one last time, in sandbox id order.
func (s *Service) FinalDiff(ctx context.Context, runID string) ([]SandboxChanges, []Blob, error) {
	if !runIDPattern.MatchString(runID) {
		return nil, nil, fmt.Errorf("%w: run id %q", ErrInvalid, runID)
	}
	s.mu.Lock()
	var ids []string
	for k := range s.sandboxes {
		if k.run == runID {
			ids = append(ids, k.sandbox)
		}
	}
	s.mu.Unlock()
	if len(ids) == 0 {
		return nil, nil, fmt.Errorf("%w: run %s has no sandboxes in this sandboxd", ErrNotFound, runID)
	}
	slices.Sort(ids)

	budget := s.cfg.ContentBudget
	var blobs []Blob
	var out []SandboxChanges
	for _, id := range ids {
		sb, err := s.lookup(runID, id)
		if err != nil {
			return nil, nil, err
		}
		sb.mu.Lock()
		changes, err := s.finalDiff(ctx, sb, &budget, &blobs)
		sb.mu.Unlock()
		if err != nil {
			return nil, nil, fmt.Errorf("final diff of sandbox %s: %w", id, err)
		}
		out = append(out, SandboxChanges{SandboxID: id, Changes: changes})
	}
	return out, blobs, nil
}

func (s *Service) finalDiff(ctx context.Context, sb *sandbox, budget *int64, blobs *[]Blob) ([]Change, error) {
	now, err := fsdiff.Walk(sb.roots, sb.manifest)
	if err != nil {
		return nil, err
	}
	procs, err := s.processes(ctx, sb)
	if err != nil {
		return nil, err
	}
	changes := s.attribute(sb, fsdiff.Diff(sb.manifest, now), true, sb.liveCalls(procs), budget, blobs)
	sb.manifest = now
	return changes, nil
}

// ReadFile reads a file inside a sandbox without executing anything there.
func (s *Service) ReadFile(ctx context.Context, runID, sandboxID, filePath string, maxBytes int64) ([]byte, int64, error) {
	if !path.IsAbs(filePath) {
		return nil, 0, fmt.Errorf("%w: path %q is not absolute", ErrInvalid, filePath)
	}
	if maxBytes <= 0 {
		maxBytes = s.cfg.ContentLimit
	}
	sb, err := s.lookup(runID, sandboxID)
	if err != nil {
		return nil, 0, err
	}
	content, size, err := s.drv.ReadFile(ctx, sb.container, filePath, maxBytes)
	if errors.Is(err, driver.ErrNotFound) {
		return nil, 0, fmt.Errorf("%w: %s in sandbox %s of run %s", ErrNotFound, filePath, sandboxID, runID)
	}
	return content, size, err
}

func (s *Service) lookup(runID, sandboxID string) (*sandbox, error) {
	if err := validateIDs(runID, sandboxID); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	sb, ok := s.sandboxes[key{runID, sandboxID}]
	if !ok {
		return nil, fmt.Errorf("%w: sandbox %s of run %s. Create it first; sandboxd forgets sandboxes when it restarts", ErrNotFound, sandboxID, runID)
	}
	return sb, nil
}

func validateIDs(runID, sandboxID string) error {
	if !runIDPattern.MatchString(runID) {
		return fmt.Errorf("%w: run id %q must match %s", ErrInvalid, runID, runIDPattern)
	}
	if !sandboxIDPattern.MatchString(sandboxID) {
		return fmt.Errorf("%w: sandbox id %q must match %s", ErrInvalid, sandboxID, sandboxIDPattern)
	}
	return nil
}

// cleanMounts checks key paths and sorts them so a parent precedes its children, which is
// the order bind mounts must be applied in.
func cleanMounts(mounts []Mount) ([]Mount, error) {
	out := slices.Clone(mounts)
	seen := map[string]bool{}
	for _, m := range out {
		if !path.IsAbs(m.Path) || path.Clean(m.Path) != m.Path || m.Path == "/" {
			return nil, fmt.Errorf("%w: key path %q must be absolute, clean, and not /", ErrInvalid, m.Path)
		}
		if seen[m.Path] {
			return nil, fmt.Errorf("%w: key path %s is listed twice", ErrInvalid, m.Path)
		}
		seen[m.Path] = true
	}
	slices.SortFunc(out, func(a, b Mount) int { return strings.Compare(a.Path, b.Path) })
	return out, nil
}

// parentMount returns the closest other mount containing p, or "".
func parentMount(mounts []Mount, p string) string {
	best := ""
	for _, m := range mounts {
		if m.Path != p && under(p, m.Path) && len(m.Path) > len(best) {
			best = m.Path
		}
	}
	return best
}

func protected(mounts []Mount, p string) bool {
	return slices.ContainsFunc(mounts, func(m Mount) bool { return m.Protected && under(p, m.Path) })
}

func under(p, dir string) bool {
	return p == dir || strings.HasPrefix(p, dir+"/")
}

func checkSeeds(files []SeedFile, mounts []Mount, limit int) error {
	total := 0
	seen := map[string]bool{}
	for _, f := range files {
		if !path.IsAbs(f.Path) || path.Clean(f.Path) != f.Path {
			return fmt.Errorf("%w: seed file %q must be absolute and clean", ErrInvalid, f.Path)
		}
		if !slices.ContainsFunc(mounts, func(m Mount) bool { return strings.HasPrefix(f.Path, m.Path+"/") }) {
			return fmt.Errorf("%w: seed file %s is not inside a key path", ErrInvalid, f.Path)
		}
		if slices.ContainsFunc(mounts, func(m Mount) bool { return m.Path == f.Path }) {
			return fmt.Errorf("%w: seed file %s is a key path itself", ErrInvalid, f.Path)
		}
		if seen[f.Path] {
			return fmt.Errorf("%w: seed file %s is listed twice", ErrInvalid, f.Path)
		}
		seen[f.Path] = true
		total += len(f.Content)
	}
	if total > limit {
		return fmt.Errorf("%w: seed files hold %d bytes, over the %d byte limit", ErrInvalid, total, limit)
	}
	return nil
}

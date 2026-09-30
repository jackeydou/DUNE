// Package sandboxd creates sandboxes, runs tool calls in them, and reports what each call
// changed on disk and left running. The gRPC surface is in server.go; this file holds the
// behavior. Design: docs/services/sandboxd.md.
package sandboxd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
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
	// InlineLimit caps stdout and stderr in the exec header.
	InlineLimit int
	// OutputLimit caps the stdout or stderr kept at all.
	OutputLimit int
	// ContentLimit caps a changed file whose content is sent back, and ReadFile's default.
	ContentLimit int64
	// ContentBudget caps the file contents sent back for one Exec or FinalDiff.
	ContentBudget int64
	// KillTimeout bounds killing a timed-out command.
	KillTimeout time.Duration
}

// DefaultConfig returns the limits sandboxd uses unless flags override them.
func DefaultConfig(stateDir string) Config {
	return Config{
		StateDir:      stateDir,
		Runtime:       "runc",
		InlineLimit:   64 << 10,
		OutputLimit:   16 << 20,
		ContentLimit:  1 << 20,
		ContentBudget: 64 << 20,
		KillTimeout:   10 * time.Second,
	}
}

// Service holds every sandbox this sandboxd created. State is in memory only: after a
// restart, sandboxes of earlier runs are unknown until takeover (M2) adopts them.
type Service struct {
	cfg Config
	drv driver.Driver
	log *slog.Logger

	mu        sync.Mutex
	sandboxes map[key]*sandbox
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
}

// New returns a Service. cfg.StateDir must exist.
func New(cfg Config, drv driver.Driver, log *slog.Logger) *Service {
	return &Service{cfg: cfg, drv: drv, log: log, sandboxes: map[key]*sandbox{}}
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
}

// CreateSandbox creates and starts a sandbox and takes its first manifest. It returns the
// runtime the sandbox got.
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

	if err := s.create(ctx, req, sb, runtime); err != nil {
		s.mu.Lock()
		delete(s.sandboxes, k)
		s.mu.Unlock()
		return "", err
	}
	return runtime, nil
}

func (s *Service) create(ctx context.Context, req CreateRequest, sb *sandbox, runtime string) error {
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

	id, err := s.drv.CreateContainer(ctx, driver.ContainerSpec{
		Image:     req.Image,
		Runtime:   runtime,
		Binds:     binds,
		Resources: req.Resources,
		Labels:    labels,
	})
	if err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	sb.container = id
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

// ExecRequest is one tool call.
type ExecRequest struct {
	RunID     string
	SandboxID string
	CallID    string
	Argv      []string
	Cwd       string
	User      string
	Timeout   time.Duration
}

// Change is a file change with its attribution.
type Change struct {
	fsdiff.Change
	Protected  bool
	Ambiguous  bool
	Candidates []string
	// Content is set when the new content is among the result's blobs.
	Content bool
}

// ExecResult is everything one call produced.
type ExecResult struct {
	ExitCode   int
	TimedOut   bool
	Duration   time.Duration
	Stdout     Output
	Stderr     Output
	Background []Change
	Changes    []Change
	Processes  []driver.Process
	Blobs      []Blob
}

// Exec runs one call and reports its output, the changes around it, and what it left
// running.
func (s *Service) Exec(ctx context.Context, req ExecRequest) (ExecResult, error) {
	if len(req.Argv) == 0 {
		return ExecResult{}, fmt.Errorf("%w: call %s has an empty argv", ErrInvalid, req.CallID)
	}
	if req.Timeout <= 0 {
		return ExecResult{}, fmt.Errorf("%w: call %s has no timeout", ErrInvalid, req.CallID)
	}
	if req.CallID == "" {
		return ExecResult{}, fmt.Errorf("%w: call id is required for attribution", ErrInvalid)
	}
	sb, err := s.lookup(req.RunID, req.SandboxID)
	if err != nil {
		return ExecResult{}, err
	}
	sb.mu.Lock()
	defer sb.mu.Unlock()

	pre, err := fsdiff.Walk(sb.roots, sb.manifest)
	if err != nil {
		return ExecResult{}, err
	}
	baseline, err := s.drv.Processes(ctx, sb.container)
	if err != nil {
		return ExecResult{}, err
	}
	candidates := sb.liveCalls(baseline)
	budget := s.cfg.ContentBudget
	var blobs []Blob
	background := s.attribute(sb, fsdiff.Diff(sb.manifest, pre), true, candidates, &budget, &blobs)

	stdout := &capBuffer{limit: s.cfg.OutputLimit}
	stderrBuf := &capBuffer{limit: s.cfg.OutputLimit}
	stderr := &pidWriter{next: stderrBuf}
	start := time.Now()
	execCtx, cancel := context.WithTimeout(ctx, req.Timeout)
	code, err := s.drv.Exec(execCtx, sb.container, driver.ExecSpec{Argv: wrapArgv(req.Argv), Cwd: req.Cwd, User: req.User}, stdout, stderr)
	cancel()
	duration := time.Since(start)
	timedOut := false
	switch {
	case err == nil:
	case errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil:
		timedOut, code = true, -1
		if err := s.killTree(ctx, sb, req, stderr.pid); err != nil {
			return ExecResult{}, err
		}
	default:
		return ExecResult{}, fmt.Errorf("call %s in sandbox %s: %w", req.CallID, req.SandboxID, err)
	}
	if err := stderr.flush(); err != nil {
		return ExecResult{}, err
	}

	post, err := fsdiff.Walk(sb.roots, pre)
	if err != nil {
		return ExecResult{}, err
	}
	after, err := s.drv.Processes(ctx, sb.container)
	if err != nil {
		return ExecResult{}, err
	}
	windowCandidates := candidates
	if len(candidates) > 0 {
		windowCandidates = append(slices.Clone(candidates), req.CallID)
	}
	changes := s.attribute(sb, fsdiff.Diff(pre, post), len(candidates) > 0, windowCandidates, &budget, &blobs)
	started := sb.recordProcesses(baseline, after, req.CallID)
	sb.manifest = post

	outBlob := func(c *capBuffer) Output {
		out, blob := c.output(s.cfg.InlineLimit)
		if blob != nil {
			blobs = append(blobs, *blob)
		}
		return out
	}
	return ExecResult{
		ExitCode:   code,
		TimedOut:   timedOut,
		Duration:   duration,
		Stdout:     outBlob(stdout),
		Stderr:     outBlob(stderrBuf),
		Background: background,
		Changes:    changes,
		Processes:  started,
		Blobs:      blobs,
	}, nil
}

// killTree kills a timed-out command and its descendants from inside the sandbox, as the
// call's own user: with every capability dropped, only the same uid may signal them.
func (s *Service) killTree(ctx context.Context, sb *sandbox, req ExecRequest, pid string) error {
	if pid == "" {
		s.log.Warn("timed-out call printed no pid; its processes are left running and will be reported",
			"run_id", req.RunID, "sandbox_id", req.SandboxID, "call_id", req.CallID)
		return nil
	}
	killCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), s.cfg.KillTimeout)
	defer cancel()
	argv := []string{"/bin/sh", "-c", killTreeScript, "sh", pid}
	if _, err := s.drv.Exec(killCtx, sb.container, driver.ExecSpec{Argv: argv, User: req.User}, io.Discard, io.Discard); err != nil {
		return fmt.Errorf("kill timed-out call %s (pid %s) in sandbox %s: %w", req.CallID, pid, req.SandboxID, err)
	}
	return nil
}

func (s *Service) attribute(sb *sandbox, diff []fsdiff.Change, ambiguous bool, candidates []string, budget *int64, blobs *[]Blob) []Change {
	out := make([]Change, len(diff))
	for i, c := range diff {
		out[i] = Change{
			Change:     c,
			Protected:  protected(sb.mounts, c.Path),
			Ambiguous:  ambiguous,
			Candidates: candidates,
		}
		if c.Op == fsdiff.OpDelete || c.After.Kind != fsdiff.KindFile || c.After.Size > s.cfg.ContentLimit || c.After.Size > *budget {
			continue
		}
		data, err := os.ReadFile(c.After.HostPath)
		if err != nil {
			continue // Gone or replaced since the walk; the hash still stands.
		}
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) != c.After.SHA256 {
			continue
		}
		*budget -= int64(len(data))
		*blobs = append(*blobs, Blob{SHA256: c.After.SHA256, Data: data})
		out[i].Content = true
	}
	return out
}

// liveCalls lists the calls whose processes are still alive, sorted.
func (sb *sandbox) liveCalls(procs []driver.Process) []string {
	seen := map[string]bool{}
	for _, p := range procs {
		if call, ok := sb.live[p.PID]; ok {
			seen[call] = true
		}
	}
	return slices.Sorted(maps.Keys(seen))
}

// recordProcesses returns processes alive after the call that were not alive before it,
// and forgets processes that have exited.
func (sb *sandbox) recordProcesses(before, after []driver.Process, callID string) []driver.Process {
	existed := map[int32]bool{}
	for _, p := range before {
		existed[p.PID] = true
	}
	alive := map[int32]bool{}
	var started []driver.Process
	for _, p := range after {
		alive[p.PID] = true
		if !existed[p.PID] {
			started = append(started, p)
			sb.live[p.PID] = callID
		}
	}
	maps.DeleteFunc(sb.live, func(pid int32, _ string) bool { return !alive[pid] })
	return started
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
	procs, err := s.drv.Processes(ctx, sb.container)
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

// DestroyRun removes every container labeled with the run, including ones this process did
// not create, and the run's state directory.
func (s *Service) DestroyRun(ctx context.Context, runID string) error {
	if !runIDPattern.MatchString(runID) {
		return fmt.Errorf("%w: run id %q", ErrInvalid, runID)
	}
	ids, err := s.drv.ListByLabels(ctx, map[string]string{driver.LabelManaged: "true", driver.LabelRunID: runID})
	if err != nil {
		return err
	}
	var errs []error
	for _, id := range ids {
		errs = append(errs, s.drv.Remove(ctx, id))
	}
	s.mu.Lock()
	maps.DeleteFunc(s.sandboxes, func(k key, _ *sandbox) bool { return k.run == runID })
	s.mu.Unlock()
	if err := os.RemoveAll(filepath.Join(s.cfg.StateDir, runID)); err != nil {
		errs = append(errs, fmt.Errorf("remove state of run %s: %w", runID, err))
	}
	return errors.Join(errs...)
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

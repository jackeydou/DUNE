package sandboxd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"slices"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

// ExecRequest is one tool call.
type ExecRequest struct {
	RunID     string
	SandboxID string
	CallID    string
	Argv      []string
	Cwd       string
	User      string
	Timeout   time.Duration
	// Collect lists files outside the key paths to read, remove, and return after the command.
	Collect []string
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
	Collected  []Collected
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
	if err := checkCollect(req.Collect, sb.mounts); err != nil {
		return ExecResult{}, fmt.Errorf("call %s: %w", req.CallID, err)
	}
	sb.mu.Lock()
	defer sb.mu.Unlock()
	sb.execd = true

	pre, err := fsdiff.Walk(sb.roots, sb.manifest)
	if err != nil {
		return ExecResult{}, err
	}
	baseline, err := s.processes(ctx, sb)
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
	collected, err := s.collect(ctx, sb, req.User, req.Collect, &blobs)
	if err != nil {
		return ExecResult{}, fmt.Errorf("call %s in sandbox %s: %w", req.CallID, req.SandboxID, err)
	}

	post, err := fsdiff.Walk(sb.roots, pre)
	if err != nil {
		return ExecResult{}, err
	}
	after, err := s.processes(ctx, sb)
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
		Collected:  collected,
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
	killCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), s.cfg.HelperTimeout)
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

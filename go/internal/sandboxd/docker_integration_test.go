//go:build integration

// Runs sandboxd against the local docker daemon. Needs `busybox:latest` on the host;
// sandboxd never pulls. Run with `mise run go:test-integration`.
package sandboxd

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/driver/docker"
	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

const image = "busybox:latest"

type live struct {
	t   *testing.T
	svc *Service
	drv *docker.Driver
	run string
	n   int
}

func newLive(t *testing.T) *live {
	t.Helper()
	ctx := context.Background()
	drv, err := docker.New(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = drv.Close() })
	state, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	l := &live{
		t: t, svc: New(DefaultConfig(state), drv, slog.New(slog.DiscardHandler)), drv: drv,
		run: "it_" + strings.ToLower(strings.ReplaceAll(t.Name(), "/", "_")),
	}
	t.Cleanup(func() {
		if err := l.svc.DestroyRun(context.Background(), l.run); err != nil {
			t.Errorf("destroy: %v", err)
		}
	})
	_, err = l.svc.CreateSandbox(ctx, CreateRequest{
		RunID: l.run, SandboxID: "box", Image: image,
		Mounts: []Mount{
			{Path: "/workspace"},
			{Path: "/workspace/tests", ReadOnly: true, Protected: true},
		},
		Resources: driver.Resources{MemoryBytes: 256 << 20, Pids: 128},
	})
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func (l *live) sh(script string, timeout time.Duration) ExecResult {
	l.t.Helper()
	l.n++
	res, err := l.svc.Exec(context.Background(), ExecRequest{
		RunID: l.run, SandboxID: "box", CallID: "call_" + string(rune('0'+l.n)),
		Argv: []string{"sh", "-c", script}, Timeout: timeout,
	})
	if err != nil {
		l.t.Fatal(err)
	}
	return res
}

func TestLiveExecReportsOutputChangesAndExitCode(t *testing.T) {
	l := newLive(t)

	res := l.sh("echo out; echo err >&2; echo hi > /workspace/a.txt; exit 4", 10*time.Second)

	if res.ExitCode != 4 || string(res.Stdout.Inline) != "out\n" || string(res.Stderr.Inline) != "err\n" {
		t.Fatalf("exit = %d, stdout = %q, stderr = %q", res.ExitCode, res.Stdout.Inline, res.Stderr.Inline)
	}
	if len(res.Changes) != 1 || res.Changes[0].Path != "/workspace/a.txt" || res.Changes[0].Op != fsdiff.OpCreate {
		t.Fatalf("changes = %+v", res.Changes)
	}
	if !res.Changes[0].Content || string(res.Blobs[0].Data) != "hi\n" {
		t.Fatalf("content = %v, blobs = %+v", res.Changes[0].Content, res.Blobs)
	}
}

func TestLiveReadOnlyKeyPathRefusesWrites(t *testing.T) {
	l := newLive(t)

	res := l.sh("echo x > /workspace/tests/t.py", 10*time.Second)

	if res.ExitCode == 0 || len(res.Changes) != 0 {
		t.Fatalf("exit = %d, changes = %+v; a read-only key path must refuse the write", res.ExitCode, res.Changes)
	}
}

func TestLiveSandboxIsOffline(t *testing.T) {
	l := newLive(t)

	res := l.sh("ls /sys/class/net", 10*time.Second)

	if got := strings.Fields(string(res.Stdout.Inline)); !slices.Equal(got, []string{"lo"}) {
		t.Fatalf("interfaces = %v; a sandbox has only loopback until net-gateway exists", got)
	}
}

func TestLiveBackgroundProcessIsReportedAndItsWritesAreAmbiguous(t *testing.T) {
	l := newLive(t)

	started := l.sh("(sleep 1; echo late > /workspace/late.txt; sleep 60) >/dev/null 2>&1 &", 10*time.Second)
	if len(started.Processes) == 0 {
		t.Fatal("the background subshell must be reported as a surviving process")
	}
	time.Sleep(2 * time.Second)
	next := l.sh("true", 10*time.Second)

	if len(next.Background) != 1 || next.Background[0].Path != "/workspace/late.txt" {
		t.Fatalf("background = %+v", next.Background)
	}
	if b := next.Background[0]; !b.Ambiguous || !slices.Equal(b.Candidates, []string{"call_1"}) {
		t.Fatalf("background change = %+v", b)
	}
}

func TestLiveTimeoutKillsTheCommandAndItsChildren(t *testing.T) {
	l := newLive(t)

	res := l.sh("sleep 300 & sleep 301", time.Second)

	if !res.TimedOut || res.ExitCode != -1 {
		t.Fatalf("result = %+v", res)
	}
	for _, p := range res.Processes {
		if strings.Contains(p.Cmdline, "sleep 30") {
			t.Fatalf("process %+v survived the timeout", p)
		}
	}
}

func TestLiveReadFileAndFinalDiff(t *testing.T) {
	l := newLive(t)
	ctx := context.Background()
	l.sh("echo answer > /workspace/result.txt; (sleep 1; rm /workspace/result.txt) >/dev/null 2>&1 &", 10*time.Second)

	content, size, err := l.svc.ReadFile(ctx, l.run, "box", "/workspace/result.txt", 0)
	if err != nil || string(content) != "answer\n" || size != 7 {
		t.Fatalf("read = %q, %d, %v", content, size, err)
	}
	time.Sleep(2 * time.Second)
	final, _, err := l.svc.FinalDiff(ctx, l.run)
	if err != nil {
		t.Fatal(err)
	}
	if c := final[0].Changes; len(c) != 1 || c[0].Op != fsdiff.OpDelete || !c[0].Ambiguous {
		t.Fatalf("final diff = %+v", final)
	}
}

func TestLiveDestroyRunRemovesContainersAndState(t *testing.T) {
	l := newLive(t)
	ctx := context.Background()
	labels := map[string]string{driver.LabelRunID: l.run}

	if err := l.svc.DestroyRun(ctx, l.run); err != nil {
		t.Fatal(err)
	}

	left, err := l.drv.ListByLabels(ctx, labels)
	if err != nil || len(left) != 0 {
		t.Fatalf("containers left = %v, %v", left, err)
	}
	if _, err := os.Stat(filepath.Join(l.svc.cfg.StateDir, l.run)); !os.IsNotExist(err) {
		t.Fatalf("state left: %v", err)
	}
}

func TestLiveTimeoutKillsAsAnUnprivilegedUser(t *testing.T) {
	l := newLive(t)

	res, err := l.svc.Exec(context.Background(), ExecRequest{
		RunID: l.run, SandboxID: "box", CallID: "call_nobody", User: "nobody",
		Argv: []string{"sh", "-c", "id -u; sleep 302"}, Timeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || string(res.Stdout.Inline) != "65534\n" {
		t.Fatalf("result = %+v, stdout = %q", res, res.Stdout.Inline)
	}
	for _, p := range res.Processes {
		if strings.Contains(p.Cmdline, "sleep 302") {
			t.Fatalf("process %+v survived; the kill must run as the call's user", p)
		}
	}
}

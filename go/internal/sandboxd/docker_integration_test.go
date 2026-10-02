//go:build integration

// Runs sandboxd against the local docker daemon. Needs `busybox:latest` on the host;
// sandboxd never pulls. Run with `mise run go:test-integration`. SWARMEVAL_IT_RUNTIME picks
// runc or runsc; the default is sandboxd's, auto.
package sandboxd

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	goruntime "runtime"
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
	cfg := DefaultConfig(state)
	if rt := os.Getenv("SWARMEVAL_IT_RUNTIME"); rt != "" {
		cfg.Runtime = rt
	}
	l := &live{
		t: t, svc: New(cfg, drv, slog.New(slog.DiscardHandler)), drv: drv,
		run: "it_" + strings.ToLower(strings.ReplaceAll(t.Name(), "/", "_")),
	}
	t.Cleanup(func() {
		if err := l.svc.DestroyRun(context.Background(), l.run); err != nil {
			t.Errorf("destroy: %v", err)
		}
	})
	if err := l.svc.CreateRun(ctx, l.run, []string{"box"}); err != nil {
		t.Fatal(err)
	}
	_, err = l.svc.CreateSandbox(ctx, CreateRequest{
		RunID: l.run, SandboxID: "box", Image: image,
		Mounts: []Mount{
			{Path: "/workspace"},
			{Path: "/workspace/tests", ReadOnly: true, Protected: true},
			{Path: "/tmp"}, // world-writable in busybox: where os_users can write a key path
		},
		Resources: driver.Resources{MemoryBytes: 256 << 20, Pids: 128},
		Users:     []string{"qa", "dev"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func (l *live) sh(script string, timeout time.Duration) ExecResult {
	l.t.Helper()
	return l.as("", script, timeout)
}

func (l *live) as(user, script string, timeout time.Duration) ExecResult {
	l.t.Helper()
	l.n++
	res, err := l.svc.Exec(context.Background(), ExecRequest{
		RunID: l.run, SandboxID: "box", CallID: "call_" + string(rune('0'+l.n)),
		Argv: []string{"sh", "-c", script}, User: user, Timeout: timeout,
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

func TestLiveSandboxRoutesOnlyToItsGatewayWhichNobodyHoldsYet(t *testing.T) {
	l := newLive(t)

	res := l.sh("ip -4 route; cat /etc/resolv.conf; nc -w 2 1.1.1.1 80 </dev/null && echo CONNECTED", 20*time.Second)

	out := string(res.Stdout.Inline)
	route := regexp.MustCompile(`default via (10\.231\.\d+\.\d+)`).FindStringSubmatch(out)
	if route == nil {
		t.Fatalf("output = %q; the sandbox's default route must point at its network's gateway", out)
	}
	if !strings.Contains(out, "nameserver "+route[1]+"\n") {
		t.Fatalf("output = %q; resolv.conf must name the gateway %s", out, route[1])
	}
	if strings.Contains(out, "CONNECTED") {
		t.Fatal("a sandbox reached 1.1.1.1 with no net-gateway on its network")
	}
}

func TestLiveOSUsersAreSeparatedByFilePermissions(t *testing.T) {
	if goruntime.GOOS == "darwin" {
		t.Skip("Docker Desktop and OrbStack on macOS enforce neither owners nor modes on bind mounts; run on Linux")
	}
	l := newLive(t)

	mine := l.as("qa", "id -un; echo secret > /tmp/q.txt; chmod 600 /tmp/q.txt; echo home > ~/h.txt && echo HOME-OK", 10*time.Second)
	theirs := l.as("dev", "cat /tmp/q.txt || echo DENIED-READ; ls /home/qa || echo DENIED-HOME; echo x >> /tmp/q.txt || echo DENIED-WRITE", 10*time.Second)

	if got := string(mine.Stdout.Inline); got != "qa\nHOME-OK\n" {
		t.Fatalf("qa's call printed %q", got)
	}
	if len(mine.Changes) != 1 || mine.Changes[0].Path != "/tmp/q.txt" {
		t.Fatalf("qa's changes = %+v", mine.Changes)
	}
	if uid := mine.Changes[0].After.UID; uid != 1001 {
		t.Fatalf("/tmp/q.txt is owned by uid %d on the host side; want qa's 1001", uid)
	}
	out := string(theirs.Stdout.Inline)
	for _, want := range []string{"DENIED-READ", "DENIED-HOME", "DENIED-WRITE"} {
		if !strings.Contains(out, want) {
			t.Errorf("dev's call printed %q; want %s", out, want)
		}
	}
	if strings.Contains(out, "secret") || len(theirs.Changes) != 0 {
		t.Fatalf("dev got through: stdout %q, changes %+v", out, theirs.Changes)
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

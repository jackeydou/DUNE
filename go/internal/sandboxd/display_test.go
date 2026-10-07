package sandboxd

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"slices"
	"testing"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// newDisplayFixture creates a sandbox with a display; the fake's display stack is pid 50, run
// by swarmdisplay, beside the container's own sleep.
func newDisplayFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t)
	idle := driver.Process{PID: 1, PPID: 0, User: "root", Cmdline: "sleep infinity"}
	stack := driver.Process{PID: 50, PPID: 1, User: DisplayUser, Cmdline: "swarm-display daemon"}
	f.drv.procs = []driver.Process{idle}
	f.drv.display = func(stdout io.Writer) (int, error) {
		f.drv.procs = []driver.Process{idle, stack}
		_, err := io.WriteString(stdout, "50\n")
		return 0, err
	}
	if err := f.svc.CreateRun(context.Background(), "run_2", []string{"desk"}); err != nil {
		t.Fatal(err)
	}
	_, err := f.svc.CreateSandbox(context.Background(), CreateRequest{
		RunID: "run_2", SandboxID: "desk", Image: "img",
		Mounts:  []Mount{{Path: "/workspace"}},
		Display: &Display{Width: 1024, Height: 768, URL: "http://127.0.0.1:8080/"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func TestDisplayStartsAsTheDisplayUserWithItsSize(t *testing.T) {
	f := newDisplayFixture(t)

	start := f.drv.execs[0]
	want := []string{"swarm-display", "start", "--width", "1024", "--height", "768", "--url", "http://127.0.0.1:8080/"}
	if start.User != DisplayUser || !slices.Equal(start.Argv, want) {
		t.Fatalf("start = %+v, want %v as %s", start, want, DisplayUser)
	}
}

func TestDisplayProcessesAreNeitherReportedNorAttributed(t *testing.T) {
	f := newDisplayFixture(t)
	idle := f.drv.procs[0]
	stack := f.drv.procs[1]
	renderer := driver.Process{PID: 61, PPID: 50, User: DisplayUser, Cmdline: "chromium --type=renderer"}
	server := driver.Process{PID: 70, PPID: 1, User: "agent", Cmdline: "python -m http.server"}

	f.drv.handler = func(context.Context, driver.ExecSpec, io.Writer, io.Writer) (int, error) {
		f.drv.procs = []driver.Process{idle, stack, renderer, server}
		return 0, nil
	}
	res, err := f.svc.Exec(context.Background(), ExecRequest{RunID: "run_2", SandboxID: "desk", CallID: "call_1", Argv: []string{"true"}, Timeout: 5e9})
	if err != nil {
		t.Fatal(err)
	}

	if len(res.Processes) != 1 || res.Processes[0].PID != 70 {
		t.Fatalf("processes = %+v; the renderer is the display's, the server the call's", res.Processes)
	}
}

func TestDisplayStartFailureRemovesTheContainer(t *testing.T) {
	cases := map[string]func(io.Writer) (int, error){
		"exit":   func(io.Writer) (int, error) { return 1, nil },
		"no pid": func(w io.Writer) (int, error) { _, err := io.WriteString(w, "ready\n"); return 0, err },
		"gone":   func(w io.Writer) (int, error) { _, err := io.WriteString(w, "99\n"); return 0, err },
	}
	for name, play := range cases {
		f := newFixture(t)
		f.drv.display = play
		if err := f.svc.CreateRun(context.Background(), "run_2", []string{"desk"}); err != nil {
			t.Fatal(err)
		}
		_, err := f.svc.CreateSandbox(context.Background(), CreateRequest{
			RunID: "run_2", SandboxID: "desk", Image: "img", Display: &Display{Width: 1024, Height: 768},
		})
		if err == nil {
			t.Errorf("%s: CreateSandbox succeeded", name)
			continue
		}
		if !slices.Contains(f.drv.removed, driver.ContainerID("c_desk")) {
			t.Errorf("%s: container not removed after %v", name, err)
		}
	}
}

func TestDisplayIsRefusedWhenItCannotWork(t *testing.T) {
	cases := map[string]CreateRequest{
		"too small":           {Display: &Display{Width: 100, Height: 768}},
		"too large":           {Display: &Display{Width: 1024, Height: 4000}},
		"url with newline":    {Display: &Display{Width: 1024, Height: 768, URL: "http://x/\nevil"}},
		"key path at its dir": {Display: &Display{Width: 1024, Height: 768}, Mounts: []Mount{{Path: DisplayDir}}},
		"key path above it":   {Display: &Display{Width: 1024, Height: 768}, Mounts: []Mount{{Path: "/run"}}},
		"key path at root":    {Display: &Display{Width: 1024, Height: 768}, Mounts: []Mount{{Path: "/"}}},
	}
	for name, req := range cases {
		svc := New(DefaultConfig(t.TempDir()), &fakeDriver{}, slog.New(slog.DiscardHandler))
		req.RunID, req.SandboxID, req.Image = "run_1", "desk", "img"
		if err := svc.CreateRun(context.Background(), "run_1", []string{"desk"}); err != nil {
			t.Fatal(err)
		}
		if _, err := svc.CreateSandbox(context.Background(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: err = %v, want ErrInvalid", name, err)
		}
	}
}

func TestExecCollectsAndRemovesFiles(t *testing.T) {
	f := newFixture(t)
	png := []byte("\x89PNG fake")
	f.drv.files = map[string][]byte{"/run/swarm-display/out/screenshot.png": png}

	res, err := f.svc.Exec(context.Background(), ExecRequest{
		RunID: "run_1", SandboxID: "box", CallID: "call_1", Argv: []string{"true"}, Timeout: 5e9,
		Collect: []string{"/run/swarm-display/out/screenshot.png", "/run/swarm-display/out/none.png"},
	})
	if err != nil {
		t.Fatal(err)
	}

	if len(res.Collected) != 2 {
		t.Fatalf("collected = %+v", res.Collected)
	}
	got, missing := res.Collected[0], res.Collected[1]
	if got.Missing || got.Size != int64(len(png)) || got.SHA256 == "" || !missing.Missing {
		t.Fatalf("collected = %+v", res.Collected)
	}
	i := slices.IndexFunc(res.Blobs, func(b Blob) bool { return b.SHA256 == got.SHA256 })
	if i < 0 || string(res.Blobs[i].Data) != string(png) {
		t.Fatalf("blobs = %v; the collected file must follow as a blob", res.Blobs)
	}
	if _, left := f.drv.files["/run/swarm-display/out/screenshot.png"]; left {
		t.Fatal("collected file was not removed")
	}
}

func TestExecCollectOverTheLimitIsRemovedButNotSent(t *testing.T) {
	f := newFixture(t)
	f.svc.cfg.CollectLimit = 4
	f.drv.files = map[string][]byte{"/run/out.png": []byte("too large")}

	res, err := f.svc.Exec(context.Background(), ExecRequest{
		RunID: "run_1", SandboxID: "box", CallID: "call_1", Argv: []string{"true"}, Timeout: 5e9,
		Collect: []string{"/run/out.png"},
	})
	if err != nil {
		t.Fatal(err)
	}

	if c := res.Collected[0]; c.Missing || c.Size != 9 || c.SHA256 != "" {
		t.Fatalf("collected = %+v", c)
	}
}

func TestExecRefusesCollectPathsInKeyPaths(t *testing.T) {
	f := newFixture(t)
	for _, p := range []string{"/workspace/shot.png", "/", "relative.png", "/run/../workspace/x"} {
		_, err := f.svc.Exec(context.Background(), ExecRequest{
			RunID: "run_1", SandboxID: "box", CallID: "call_1", Argv: []string{"true"}, Timeout: 5e9,
			Collect: []string{p},
		})
		if !errors.Is(err, ErrInvalid) {
			t.Errorf("collect %q: err = %v, want ErrInvalid", p, err)
		}
	}
}

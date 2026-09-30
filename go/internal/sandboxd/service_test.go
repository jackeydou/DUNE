package sandboxd

import (
	"archive/tar"
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

// fakeDriver stands in for docker. Its handler plays the command: it writes to the host
// directory the sandbox's key path is mounted from, and to stdout and stderr.
type fakeDriver struct {
	mu       sync.Mutex
	image    map[string]*tarEntry // path in image → a single file under it
	handler  func(ctx context.Context, spec driver.ExecSpec, stdout, stderr io.Writer) (int, error)
	procs    []driver.Process
	execs    []driver.ExecSpec
	created  []driver.ContainerSpec
	removed  []driver.ContainerID
	runtimes []string
}

func (f *fakeDriver) Runtimes(context.Context) ([]string, error) { return f.runtimes, nil }

func (f *fakeDriver) ExportImagePath(_ context.Context, _, p string, _ map[string]string) (io.ReadCloser, error) {
	e, ok := f.image[p]
	if !ok {
		return nil, driver.ErrNotFound
	}
	return io.NopCloser(archiveReader(*e)), nil
}

func archiveReader(e tarEntry) io.Reader {
	r, w := io.Pipe()
	go func() {
		tw := tar.NewWriter(w)
		_ = tw.WriteHeader(&tar.Header{Name: e.name, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(e.body))})
		_, _ = tw.Write([]byte(e.body))
		_ = w.CloseWithError(tw.Close())
	}()
	return r
}

func (f *fakeDriver) CreateContainer(_ context.Context, spec driver.ContainerSpec) (driver.ContainerID, error) {
	f.created = append(f.created, spec)
	return driver.ContainerID("c_" + spec.Labels[driver.LabelSandboxID]), nil
}

func (f *fakeDriver) Exec(ctx context.Context, _ driver.ContainerID, spec driver.ExecSpec, stdout, stderr io.Writer) (int, error) {
	f.mu.Lock()
	f.execs = append(f.execs, spec)
	f.mu.Unlock()
	if spec.Argv[2] == killTreeScript {
		return 0, nil
	}
	if _, err := io.WriteString(stderr, "42\n"); err != nil {
		return 0, err
	}
	return f.handler(ctx, spec, stdout, stderr)
}

func (f *fakeDriver) Processes(context.Context, driver.ContainerID) ([]driver.Process, error) {
	return f.procs, nil
}

func (f *fakeDriver) ReadFile(context.Context, driver.ContainerID, string, int64) ([]byte, int64, error) {
	return nil, 0, driver.ErrNotFound
}

func (f *fakeDriver) ListByLabels(_ context.Context, labels map[string]string) ([]driver.ContainerID, error) {
	var out []driver.ContainerID
	for _, c := range f.created {
		if c.Labels[driver.LabelRunID] == labels[driver.LabelRunID] {
			out = append(out, driver.ContainerID("c_"+c.Labels[driver.LabelSandboxID]))
		}
	}
	return out, nil
}

func (f *fakeDriver) Remove(_ context.Context, id driver.ContainerID) error {
	f.removed = append(f.removed, id)
	return nil
}

type fixture struct {
	svc       *Service
	drv       *fakeDriver
	state     string
	workspace string // host dir behind /workspace
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	state := t.TempDir()
	drv := &fakeDriver{
		image: map[string]*tarEntry{"/workspace": {name: "workspace/seed.txt", body: "seed"}},
		handler: func(context.Context, driver.ExecSpec, io.Writer, io.Writer) (int, error) {
			return 0, nil
		},
	}
	svc := New(DefaultConfig(state), drv, slog.New(slog.DiscardHandler))
	_, err := svc.CreateSandbox(context.Background(), CreateRequest{
		RunID: "run_1", SandboxID: "box", Image: "img",
		Mounts: []Mount{
			{Path: "/workspace/tests", ReadOnly: true, Protected: true},
			{Path: "/workspace"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	return &fixture{svc: svc, drv: drv, state: state, workspace: filepath.Join(state, "run_1", "box", "fs", "workspace")}
}

func (f *fixture) exec(t *testing.T, call string, handler func(ctx context.Context, stdout io.Writer) (int, error)) ExecResult {
	t.Helper()
	f.drv.handler = func(ctx context.Context, _ driver.ExecSpec, stdout, _ io.Writer) (int, error) {
		return handler(ctx, stdout)
	}
	res, err := f.svc.Exec(context.Background(), ExecRequest{
		RunID: "run_1", SandboxID: "box", CallID: call, Argv: []string{"true"}, User: "agent", Timeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func paths(cs []Change) []string {
	out := make([]string, len(cs))
	for i, c := range cs {
		out[i] = c.Path
	}
	return out
}

func TestCreateSandboxPopulatesKeyPathsAndMountsParentsFirst(t *testing.T) {
	f := newFixture(t)

	if got, _ := os.ReadFile(filepath.Join(f.workspace, "seed.txt")); string(got) != "seed" {
		t.Fatalf("seed.txt = %q; key paths start with the image's content", got)
	}
	binds := f.drv.created[0].Binds
	if len(binds) != 2 || binds[0].ContainerPath != "/workspace" || !binds[1].ReadOnly {
		t.Fatalf("binds = %+v; /workspace must be mounted before its read-only child", binds)
	}
	if f.drv.created[0].Runtime != "runc" || f.drv.created[0].Labels[driver.LabelManaged] != "true" {
		t.Fatalf("spec = %+v", f.drv.created[0])
	}
}

func TestExecAttributesChangesToTheCallAndReturnsContent(t *testing.T) {
	f := newFixture(t)

	res := f.exec(t, "call_1", func(_ context.Context, stdout io.Writer) (int, error) {
		_, _ = io.WriteString(stdout, "done\n")
		return 3, os.WriteFile(filepath.Join(f.workspace, "tests", "test_a.py"), []byte("assert True"), 0o644)
	})

	if res.ExitCode != 3 || string(res.Stdout.Inline) != "done\n" || len(res.Stderr.Inline) != 0 {
		t.Fatalf("result = %+v; the pid line must not reach stderr", res)
	}
	if got := paths(res.Changes); !slices.Equal(got, []string{"/workspace/tests/test_a.py"}) {
		t.Fatalf("changes = %v", got)
	}
	c := res.Changes[0]
	if c.Op != fsdiff.OpCreate || c.Ambiguous || !c.Protected || !c.Content {
		t.Fatalf("change = %+v", c)
	}
	if len(res.Blobs) != 1 || string(res.Blobs[0].Data) != "assert True" || res.Blobs[0].SHA256 != c.After.SHA256 {
		t.Fatalf("blobs = %+v", res.Blobs)
	}
	if got := f.drv.execs[0]; got.User != "agent" || got.Argv[0] != "/bin/sh" || got.Argv[len(got.Argv)-1] != "true" {
		t.Fatalf("exec spec = %+v", got)
	}
}

func TestChangesBetweenCallsAreBackgroundAndNameTheLiveCall(t *testing.T) {
	f := newFixture(t)
	server := driver.Process{PID: 900, PPID: 1, User: "agent", Cmdline: "python server.py"}
	f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) {
		f.drv.procs = []driver.Process{server}
		return 0, nil
	})

	if err := os.WriteFile(filepath.Join(f.workspace, "log.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	res := f.exec(t, "call_2", func(context.Context, io.Writer) (int, error) {
		return 0, os.WriteFile(filepath.Join(f.workspace, "mine.txt"), []byte("y"), 0o644)
	})

	if len(res.Background) != 1 || res.Background[0].Path != "/workspace/log.txt" {
		t.Fatalf("background = %+v", res.Background)
	}
	if b := res.Background[0]; !b.Ambiguous || !slices.Equal(b.Candidates, []string{"call_1"}) {
		t.Fatalf("background change = %+v; it must be ambiguous and name call_1", b)
	}
	if c := res.Changes[0]; !c.Ambiguous || !slices.Equal(c.Candidates, []string{"call_1", "call_2"}) {
		t.Fatalf("window change = %+v; call_1's process was alive during call_2", c)
	}
}

func TestExecReportsProcessesTheCallLeftRunning(t *testing.T) {
	f := newFixture(t)
	idle := driver.Process{PID: 10, PPID: 1, User: "root", Cmdline: "sleep infinity"}
	f.drv.procs = []driver.Process{idle}

	res := f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) {
		f.drv.procs = []driver.Process{idle, {PID: 77, PPID: 1, User: "agent", Cmdline: "nc -l 8080"}}
		return 0, nil
	})

	if len(res.Processes) != 1 || res.Processes[0].PID != 77 {
		t.Fatalf("processes = %+v", res.Processes)
	}
}

func TestTimeoutKillsTheTreeAsTheCallsUser(t *testing.T) {
	f := newFixture(t)

	res := f.exec(t, "call_1", func(ctx context.Context, _ io.Writer) (int, error) {
		<-ctx.Done()
		return 0, ctx.Err()
	})

	if !res.TimedOut || res.ExitCode != -1 {
		t.Fatalf("result = %+v", res)
	}
	kill := f.drv.execs[len(f.drv.execs)-1]
	if kill.Argv[2] != killTreeScript || kill.Argv[4] != "42" || kill.User != "agent" {
		t.Fatalf("kill exec = %+v; want the kill script for pid 42 as user agent", kill)
	}
}

func TestLargeFilesAreReportedByHashOnly(t *testing.T) {
	f := newFixture(t)
	f.svc.cfg.ContentLimit = 4

	res := f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) {
		return 0, os.WriteFile(filepath.Join(f.workspace, "big.bin"), []byte("12345"), 0o644)
	})

	if c := res.Changes[0]; c.Content || c.After.SHA256 == "" || len(res.Blobs) != 0 {
		t.Fatalf("change = %+v, blobs = %d", c, len(res.Blobs))
	}
}

func TestFinalDiffCatchesLateWritesAsAmbiguous(t *testing.T) {
	f := newFixture(t)
	f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) { return 0, nil })
	if err := os.Remove(filepath.Join(f.workspace, "seed.txt")); err != nil {
		t.Fatal(err)
	}

	got, _, err := f.svc.FinalDiff(context.Background(), "run_1")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || len(got[0].Changes) != 1 {
		t.Fatalf("final diff = %+v", got)
	}
	if c := got[0].Changes[0]; c.Op != fsdiff.OpDelete || !c.Ambiguous {
		t.Fatalf("change = %+v", c)
	}
}

func TestDestroyRunRemovesContainersAndState(t *testing.T) {
	f := newFixture(t)

	if err := f.svc.DestroyRun(context.Background(), "run_1"); err != nil {
		t.Fatal(err)
	}

	if !slices.Equal(f.drv.removed, []driver.ContainerID{"c_box"}) {
		t.Fatalf("removed = %v", f.drv.removed)
	}
	if _, err := os.Stat(filepath.Join(f.state, "run_1")); !os.IsNotExist(err) {
		t.Fatalf("state dir still there: %v", err)
	}
	if _, err := f.svc.Exec(context.Background(), ExecRequest{
		RunID: "run_1", SandboxID: "box", CallID: "c", Argv: []string{"true"}, Timeout: time.Second,
	}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("exec after destroy: %v", err)
	}
}

func TestRequestsAreValidated(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()

	cases := map[string]error{
		"duplicate sandbox": func() error {
			_, err := f.svc.CreateSandbox(ctx, CreateRequest{RunID: "run_1", SandboxID: "box", Image: "img"})
			return err
		}(),
		"run id with a slash": func() error {
			_, err := f.svc.CreateSandbox(ctx, CreateRequest{RunID: "../x", SandboxID: "box", Image: "img"})
			return err
		}(),
		"relative key path": func() error {
			_, err := f.svc.CreateSandbox(ctx, CreateRequest{RunID: "run_2", SandboxID: "box", Image: "img", Mounts: []Mount{{Path: "work"}}})
			return err
		}(),
		"no timeout": func() error {
			_, err := f.svc.Exec(ctx, ExecRequest{RunID: "run_1", SandboxID: "box", CallID: "c", Argv: []string{"true"}})
			return err
		}(),
		"unknown sandbox": func() error {
			_, err := f.svc.Exec(ctx, ExecRequest{RunID: "run_1", SandboxID: "nope", CallID: "c", Argv: []string{"true"}, Timeout: time.Second})
			return err
		}(),
	}
	want := map[string]error{
		"duplicate sandbox":   ErrExists,
		"run id with a slash": ErrInvalid,
		"relative key path":   ErrInvalid,
		"no timeout":          ErrInvalid,
		"unknown sandbox":     ErrNotFound,
	}
	for name, err := range cases {
		if !errors.Is(err, want[name]) {
			t.Errorf("%s: err = %v, want %v", name, err, want[name])
		}
	}
}

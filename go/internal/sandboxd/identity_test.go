package sandboxd

import (
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"slices"
	"strings"
	"testing"

	"github.com/jackeydou/DUNE/go/internal/driver"
	sandboxv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/sandbox/v1"
)

func createWithIdentity(t *testing.T, req CreateRequest) (*fakeDriver, error) {
	t.Helper()
	drv := &fakeDriver{}
	svc := New(DefaultConfig(t.TempDir()), drv, slog.New(slog.DiscardHandler))
	if err := svc.CreateRun(context.Background(), "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}
	req.RunID, req.SandboxID, req.Image = "run_1", "box", "img"
	_, err := svc.CreateSandbox(context.Background(), req)
	return drv, err
}

func TestIdentityReachesTheContainer(t *testing.T) {
	drv, err := createWithIdentity(t, CreateRequest{
		Mounts:    []Mount{{Path: "/workspace"}},
		Env:       map[string]string{"INSTANCE_ID": "0123", "A_FIRST": "x=y"},
		Hostname:  "0123456789abcdef0123456789abcdef",
		MachineID: "0123456789abcdef0123456789abcdef",
	})
	if err != nil {
		t.Fatal(err)
	}

	spec := drv.created[0]
	if !slices.Equal(spec.Env, []string{"A_FIRST=x=y", "INSTANCE_ID=0123"}) {
		t.Fatalf("env = %v; want NAME=value pairs, sorted", spec.Env)
	}
	if spec.Hostname != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("hostname = %q", spec.Hostname)
	}
	want := driver.File{Path: "/etc/machine-id", Content: []byte("0123456789abcdef0123456789abcdef\n"), Mode: 0o444}
	i := slices.IndexFunc(spec.Files, func(f driver.File) bool { return f.Path == want.Path })
	if i < 0 || string(spec.Files[i].Content) != string(want.Content) || spec.Files[i].Mode != want.Mode {
		t.Fatalf("files = %+v; want %+v written before start", spec.Files, want)
	}
}

func TestNoIdentityLeavesTheBackendDefaults(t *testing.T) {
	drv, err := createWithIdentity(t, CreateRequest{})
	if err != nil {
		t.Fatal(err)
	}

	spec := drv.created[0]
	if len(spec.Env) != 0 || spec.Hostname != "" || len(spec.Files) != 0 {
		t.Fatalf("spec = %+v; nothing asked for, nothing set", spec)
	}
}

func TestIdentityIsValidated(t *testing.T) {
	cases := map[string]CreateRequest{
		"env name with a dash":      {Env: map[string]string{"BAD-NAME": "x"}},
		"env name with a digit":     {Env: map[string]string{"1X": "x"}},
		"env value with a NUL":      {Env: map[string]string{"X": "a\x00b"}},
		"hostname with uppercase":   {Hostname: "Box"},
		"hostname with underscore":  {Hostname: "my_box"},
		"hostname ending in a dash": {Hostname: "box-"},
		"hostname of 64 characters": {Hostname: strings.Repeat("a", 64)},
		"machine id too short":      {MachineID: "0123"},
		"machine id in uppercase":   {MachineID: strings.Repeat("A", 32)},
		"machine id under a key path": {
			Mounts:    []Mount{{Path: "/etc"}},
			MachineID: strings.Repeat("a", 32),
		},
	}
	for name, req := range cases {
		if _, err := createWithIdentity(t, req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: err = %v, want ErrInvalid", name, err)
		}
	}
	if _, err := createWithIdentity(t, CreateRequest{Hostname: strings.Repeat("a", 63)}); err != nil {
		t.Errorf("a 63-character hostname: %v", err)
	}
}

func TestMachineIDIsWrittenAfterAddedUsers(t *testing.T) {
	drv := &fakeDriver{etc: []tarEntry{{name: "etc/passwd", body: "root:x:0:0::/root:/bin/sh\n"}}}
	svc := New(DefaultConfig(t.TempDir()), drv, slog.New(slog.DiscardHandler))
	if err := svc.CreateRun(context.Background(), "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}
	_, err := svc.CreateSandbox(context.Background(), CreateRequest{
		RunID: "run_1", SandboxID: "box", Image: "img", Users: []string{"qa"},
		MachineID: strings.Repeat("b", 32),
	})
	if err != nil {
		t.Fatal(err)
	}

	files := drv.created[0].Files
	last := files[len(files)-1]
	if last.Path != "/etc/machine-id" || last.Mode != fs.FileMode(0o444) {
		t.Fatalf("files = %+v; the machine id joins the users' files", files)
	}
	if !slices.ContainsFunc(files, func(f driver.File) bool { return f.Path == "/home/qa" }) {
		t.Fatalf("files = %+v; the user's home is still there", files)
	}
}

func TestServerPassesIdentityThrough(t *testing.T) {
	drv := &fakeDriver{}
	svc := New(DefaultConfig(t.TempDir()), drv, slog.New(slog.DiscardHandler))
	if err := svc.CreateRun(context.Background(), "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}
	srv := NewServer(svc, slog.New(slog.DiscardHandler))

	_, err := srv.CreateSandbox(context.Background(), &sandboxv1.CreateSandboxRequest{
		RunId: "run_1", SandboxId: "box", Image: "img",
		Env: map[string]string{"INSTANCE_ID": "x"}, Hostname: "h-1", MachineId: strings.Repeat("c", 32),
	})
	if err != nil {
		t.Fatal(err)
	}

	spec := drv.created[0]
	if !slices.Equal(spec.Env, []string{"INSTANCE_ID=x"}) || spec.Hostname != "h-1" || len(spec.Files) != 1 {
		t.Fatalf("spec = %+v", spec)
	}
}

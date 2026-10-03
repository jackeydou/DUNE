package sandboxd

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestRestoreFilesBecomeTheBaseline(t *testing.T) {
	f := newFixture(t)

	err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box",
		Remove: []string{"/workspace/seed.txt", "/workspace/never/there"},
		Dirs:   []RestoreDir{{Path: "/workspace/out/deep", Mode: 0o700}},
		Files: []SeedFile{
			{Path: "/workspace/out/deep/note.txt", Content: []byte("hi"), Mode: 0o600},
			{Path: "/workspace/tests/test_a.py", Content: []byte("assert 1")},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(filepath.Join(f.workspace, "seed.txt")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("seed.txt: %v; a removed path is gone", err)
	}
	info, err := os.Stat(filepath.Join(f.workspace, "out", "deep"))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("out/deep: %v, %v", info, err)
	}
	if got, _ := os.ReadFile(filepath.Join(f.workspace, "out", "deep", "note.txt")); string(got) != "hi" {
		t.Fatalf("note.txt = %q", got)
	}
	res := f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) { return 0, nil })
	if len(res.Changes) != 0 || len(res.Background) != 0 {
		t.Fatalf("changes = %v, background = %v; restored files belong to the baseline", paths(res.Changes), paths(res.Background))
	}
}

func TestRestoreFilesIsRefusedAfterTheFirstCommand(t *testing.T) {
	f := newFixture(t)
	f.exec(t, "call_1", func(context.Context, io.Writer) (int, error) { return 0, nil })

	err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box", Files: []SeedFile{{Path: "/workspace/x", Content: []byte("x")}},
	})
	if !errors.Is(err, ErrState) {
		t.Fatalf("err = %v, want ErrState", err)
	}
}

func TestRestorePathsOutsideKeyPathsAreRefused(t *testing.T) {
	f := newFixture(t)
	for _, req := range []RestoreRequest{
		{Remove: []string{"/etc/passwd"}},
		{Remove: []string{"/workspace"}},
		{Dirs: []RestoreDir{{Path: "/workspace/../etc"}}},
		{Files: []SeedFile{{Path: "/tmp/x"}}},
	} {
		req.RunID, req.SandboxID = "run_1", "box"
		if err := f.svc.RestoreFiles(context.Background(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%+v: err = %v, want ErrInvalid", req, err)
		}
	}
	if _, err := os.Stat(filepath.Join(f.workspace, "seed.txt")); err != nil {
		t.Fatalf("seed.txt: %v; a refused request changes nothing", err)
	}
}

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

	uid := uint32(os.Getuid())
	unowned, err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box",
		Remove: []string{"/workspace/seed.txt", "/workspace/never/there"},
		Dirs:   []RestoreDir{{Path: "/workspace/out/deep", Mode: 0o700, UID: uid}},
		Files: []RestoreFile{
			{Path: "/workspace/out/deep/note.txt", Content: []byte("hi"), Mode: 0o600, UID: uid},
			{Path: "/workspace/tests/test_a.py", Content: []byte("assert 1"), UID: uid},
		},
	})
	if err != nil || len(unowned) != 0 {
		t.Fatalf("unowned = %v, err = %v", unowned, err)
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

	_, err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box", Files: []RestoreFile{{Path: "/workspace/x", Content: []byte("x")}},
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
		{Files: []RestoreFile{{Path: "/tmp/x"}}},
	} {
		req.RunID, req.SandboxID = "run_1", "box"
		if _, err := f.svc.RestoreFiles(context.Background(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("%+v: err = %v, want ErrInvalid", req, err)
		}
	}
	if _, err := os.Stat(filepath.Join(f.workspace, "seed.txt")); err != nil {
		t.Fatalf("seed.txt: %v; a refused request changes nothing", err)
	}
}

func TestRestoreReplacesWhatIsOfAnotherKindAndNeverWritesThroughALink(t *testing.T) {
	f := newFixture(t)
	if err := os.Mkdir(filepath.Join(f.workspace, "was_dir"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.workspace, "was_file"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(f.workspace, "real"), []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("real", filepath.Join(f.workspace, "link")); err != nil {
		t.Fatal(err)
	}
	uid := uint32(os.Getuid())

	_, err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box",
		Dirs: []RestoreDir{{Path: "/workspace/was_file", UID: uid}},
		Files: []RestoreFile{
			{Path: "/workspace/was_dir", Content: []byte("now a file"), UID: uid},
			{Path: "/workspace/link", Content: []byte("now a file too"), UID: uid},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	if info, err := os.Stat(filepath.Join(f.workspace, "was_file")); err != nil || !info.IsDir() {
		t.Fatalf("was_file: %v, %v; it is a directory now", info, err)
	}
	if got, _ := os.ReadFile(filepath.Join(f.workspace, "was_dir")); string(got) != "now a file" {
		t.Fatalf("was_dir = %q", got)
	}
	if info, err := os.Lstat(filepath.Join(f.workspace, "link")); err != nil || info.Mode().Type() != 0 {
		t.Fatalf("link: %v, %v; the symlink is replaced by a regular file", info, err)
	}
	if got, _ := os.ReadFile(filepath.Join(f.workspace, "real")); string(got) != "keep" {
		t.Fatalf("real = %q; a restore must never write through a link", got)
	}
}

func TestRestoreReportsOwnersItCannotSet(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root can give any owner")
	}
	f := newFixture(t)

	unowned, err := f.svc.RestoreFiles(context.Background(), RestoreRequest{
		RunID: "run_1", SandboxID: "box",
		Files: []RestoreFile{{Path: "/workspace/theirs.txt", Content: []byte("x"), UID: uint32(os.Getuid()) + 1}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(unowned) != 1 || unowned[0] != "/workspace/theirs.txt" {
		t.Fatalf("unowned = %v", unowned)
	}
}

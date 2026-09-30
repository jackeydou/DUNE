package fsdiff

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func walk(t *testing.T, dir string, prev Manifest) Manifest {
	t.Helper()
	m, err := Walk([]Root{{HostDir: dir, ContainerPath: "/workspace"}}, prev)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

type summary struct {
	path string
	op   Op
}

func summarize(changes []Change) []summary {
	out := make([]summary, len(changes))
	for i, c := range changes {
		out[i] = summary{c.Path, c.Op}
	}
	return out
}

func assertChanges(t *testing.T, got []Change, want ...summary) {
	t.Helper()
	s := summarize(got)
	if len(s) != len(want) {
		t.Fatalf("changes = %v, want %v", s, want)
	}
	for i := range want {
		if s[i] != want[i] {
			t.Fatalf("changes = %v, want %v", s, want)
		}
	}
}

func TestWalkMapsHostPathsToSandboxPaths(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "src", "main.py"), "print(1)")

	m := walk(t, dir, nil)

	e, ok := m["/workspace/src/main.py"]
	if !ok {
		t.Fatalf("manifest = %v, want /workspace/src/main.py", m)
	}
	if e.Kind != KindFile || e.Size != 8 || e.SHA256 == "" {
		t.Fatalf("entry = %+v", e)
	}
	if m["/workspace/src"].Kind != KindDir {
		t.Fatalf("src entry = %+v, want a directory", m["/workspace/src"])
	}
	if _, ok := m["/workspace"]; ok {
		t.Fatal("the root itself is the mount point and must not be listed")
	}
}

func TestDiffReportsCreateModifyDelete(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "keep.txt"), "a")
	write(t, filepath.Join(dir, "edit.txt"), "a")
	write(t, filepath.Join(dir, "gone.txt"), "a")
	before := walk(t, dir, nil)

	write(t, filepath.Join(dir, "edit.txt"), "bb")
	write(t, filepath.Join(dir, "new", "file.txt"), "c")
	if err := os.Remove(filepath.Join(dir, "gone.txt")); err != nil {
		t.Fatal(err)
	}
	after := walk(t, dir, before)

	changes := Diff(before, after)
	assertChanges(t, changes,
		summary{"/workspace/edit.txt", OpModify},
		summary{"/workspace/gone.txt", OpDelete},
		summary{"/workspace/new", OpCreate},
		summary{"/workspace/new/file.txt", OpCreate},
	)
	edit := changes[0]
	if edit.Before.SHA256 == edit.After.SHA256 {
		t.Fatal("a modified file must have different before and after hashes")
	}
}

func TestDirectoryMtimeAloneIsNotAChange(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "d", "a.txt"), "a")
	before := walk(t, dir, nil)

	write(t, filepath.Join(dir, "d", "b.txt"), "b")

	assertChanges(t, Diff(before, walk(t, dir, before)), summary{"/workspace/d/b.txt", OpCreate})
}

func TestModeChangeIsAModify(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "run.sh")
	write(t, file, "echo")
	before := walk(t, dir, nil)

	if err := os.Chmod(file, 0o755); err != nil {
		t.Fatal(err)
	}

	assertChanges(t, Diff(before, walk(t, dir, before)), summary{"/workspace/run.sh", OpModify})
}

func TestSymlinkIsHashedByTargetAndNotFollowed(t *testing.T) {
	dir := t.TempDir()
	outside := filepath.Join(t.TempDir(), "secret")
	write(t, outside, "host data")
	if err := os.Symlink(outside, filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}

	m := walk(t, dir, nil)

	e := m["/workspace/link"]
	if e.Kind != KindSymlink || e.SHA256 == "" {
		t.Fatalf("entry = %+v, want a hashed symlink", e)
	}
	if len(m) != 1 {
		t.Fatalf("manifest = %v; the walk must not descend through the link", m)
	}
}

func TestFifoIsListedButNeverOpened(t *testing.T) {
	dir := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(dir, "pipe"), 0o644); err != nil {
		t.Fatal(err)
	}

	done := make(chan Manifest)
	go func() { done <- walk(t, dir, nil) }()
	select {
	case m := <-done:
		if e := m["/workspace/pipe"]; e.Kind != KindOther || e.SHA256 != "" {
			t.Fatalf("entry = %+v, want KindOther without hash", e)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("walk blocked on a fifo")
	}
}

func TestUnchangedMetadataReusesTheHash(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "a.txt")
	write(t, file, "a")
	before := walk(t, dir, nil)
	stale := before["/workspace/a.txt"]
	stale.SHA256 = "stale"
	before["/workspace/a.txt"] = stale

	after := walk(t, dir, before)

	if got := after["/workspace/a.txt"].SHA256; got != "stale" {
		t.Fatalf("hash = %q; an entry with unchanged metadata must not be read again", got)
	}
}

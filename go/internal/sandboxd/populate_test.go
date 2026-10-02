package sandboxd

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

type tarEntry struct {
	name, body, link string
	typ              byte
	mode             int64
}

func archive(t *testing.T, entries ...tarEntry) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, e := range entries {
		mode := e.mode
		if mode == 0 {
			mode = 0o644
		}
		h := &tar.Header{Name: e.name, Typeflag: e.typ, Mode: mode, Size: int64(len(e.body)), Linkname: e.link}
		if err := tw.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(e.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return &buf
}

func TestExtractDropsTheArchiveRootAndKeepsModes(t *testing.T) {
	dir := t.TempDir()
	a := archive(t,
		tarEntry{name: "workspace/", typ: tar.TypeDir, mode: 0o755},
		tarEntry{name: "workspace/tests/", typ: tar.TypeDir, mode: 0o755},
		tarEntry{name: "workspace/tests/test_a.py", typ: tar.TypeReg, body: "assert 1"},
		tarEntry{name: "workspace/run.sh", typ: tar.TypeReg, body: "echo", mode: 0o755},
		tarEntry{name: "workspace/latest", typ: tar.TypeSymlink, link: "tests"},
		tarEntry{name: "workspace/copy.py", typ: tar.TypeLink, link: "workspace/tests/test_a.py"},
	)

	if err := extract(a, dir); err != nil {
		t.Fatal(err)
	}

	if got, _ := os.ReadFile(filepath.Join(dir, "tests", "test_a.py")); string(got) != "assert 1" {
		t.Fatalf("test_a.py = %q", got)
	}
	if info, _ := os.Stat(filepath.Join(dir, "run.sh")); info.Mode().Perm() != 0o755 {
		t.Fatalf("run.sh mode = %v", info.Mode())
	}
	if target, _ := os.Readlink(filepath.Join(dir, "latest")); target != "tests" {
		t.Fatalf("latest -> %q", target)
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "copy.py")); string(got) != "assert 1" {
		t.Fatalf("copy.py = %q", got)
	}
}

func TestExtractCannotWriteThroughASymlinkOutOfTheDirectory(t *testing.T) {
	dir := t.TempDir()
	outside := t.TempDir()
	a := archive(t,
		tarEntry{name: "workspace/escape", typ: tar.TypeSymlink, link: outside},
		tarEntry{name: "workspace/escape/pwned", typ: tar.TypeReg, body: "x"},
	)

	if err := extract(a, dir); err == nil {
		t.Fatal("extract followed a symlink out of its directory")
	}
	if _, err := os.Stat(filepath.Join(outside, "pwned")); !os.IsNotExist(err) {
		t.Fatalf("file written outside: %v", err)
	}
}

func TestExtractCannotClimbWithDotDot(t *testing.T) {
	parent := t.TempDir()
	dir := filepath.Join(parent, "fs")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	a := archive(t, tarEntry{name: "workspace/../../pwned", typ: tar.TypeReg, body: "x"})

	_ = extract(a, dir)

	if _, err := os.Stat(filepath.Join(parent, "pwned")); !os.IsNotExist(err) {
		t.Fatalf("file written outside: %v", err)
	}
}

func TestExtractGivesTheKeyPathItsOwnModeFromTheImage(t *testing.T) {
	dir := t.TempDir()
	a := archive(t,
		tarEntry{name: "tmp/", typ: tar.TypeDir, mode: 0o1777},
		tarEntry{name: "tmp/tool", typ: tar.TypeReg, body: "x", mode: 0o2755},
	)

	if err := extract(a, dir); err != nil {
		t.Fatal(err)
	}

	if info, _ := os.Stat(dir); info.Mode() != os.ModeDir|os.ModeSticky|0o777 {
		t.Fatalf("key path mode = %v; want the image's drwxrwxrwt, or os_users cannot write /tmp", info.Mode())
	}
	if info, _ := os.Stat(filepath.Join(dir, "tool")); info.Mode() != os.ModeSetgid|0o755 {
		t.Fatalf("tool mode = %v; setgid must survive", info.Mode())
	}
}

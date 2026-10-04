package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

func TestParseCaseRef(t *testing.T) {
	for arg, want := range map[string][3]any{
		"safety/demo":    {"safety", "demo", int32(0)},
		"safety/demo@12": {"safety", "demo", int32(12)},
		"ws-1/a_b@1":     {"ws-1", "a_b", int32(1)},
	} {
		ref, err := parseCaseRef(arg)
		if err != nil {
			t.Fatalf("%s: %v", arg, err)
		}
		if ref.GetWorkspace() != want[0] || ref.GetCaseId() != want[1] || ref.GetRevision() != want[2] {
			t.Errorf("%s: got %v", arg, ref)
		}
	}
	for _, arg := range []string{"", "demo", "/demo", "safety/", "a/b/c", "safety/demo@", "safety/demo@0", "safety/demo@-1", "safety/demo@x", "safety/demo@99999999999"} {
		if ref, err := parseCaseRef(arg); err == nil {
			t.Errorf("%q: accepted as %v", arg, ref)
		}
	}
	if _, err := unpinnedCaseRef("safety/demo@2"); err == nil || !strings.Contains(err.Error(), "without a revision") {
		t.Errorf("unpinnedCaseRef took a revision: %v", err)
	}
}

func TestWriteCaseWritesFilesModesAndLinks(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "out")
	err := writeCase(dir, []*apiv1.CaseFile{
		{Path: "alias.md", LinkTarget: "prompts/a.md"},
		{Path: "case.yaml", Content: []byte("id: x\n"), Mode: 0o644},
		{Path: "prompts/a.md", Content: []byte("hi"), Mode: 0o600},
		{Path: "run.sh", Content: []byte("#!/bin/sh\n"), Mode: 0o755},
	})
	if err != nil {
		t.Fatal(err)
	}
	through, err := os.ReadFile(filepath.Join(dir, "alias.md"))
	if err != nil || string(through) != "hi" {
		t.Fatalf("alias.md: %q, %v", through, err)
	}
	if target, err := os.Readlink(filepath.Join(dir, "alias.md")); err != nil || target != filepath.FromSlash("prompts/a.md") {
		t.Fatalf("alias.md is not the link: %q, %v", target, err)
	}
	for name, mode := range map[string]os.FileMode{"prompts/a.md": 0o600, "run.sh": 0o755} {
		info, err := os.Stat(filepath.Join(dir, filepath.FromSlash(name)))
		if err != nil || info.Mode().Perm() != mode {
			t.Errorf("%s: mode %v, %v; want %v", name, info.Mode().Perm(), err, mode)
		}
	}
	// The round trip: what was pulled packs to the same four entries.
	bundle, err := pack(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range untar(t, bundle) {
		names = append(names, e.name)
	}
	if strings.Join(names, " ") != "alias.md case.yaml prompts/a.md run.sh" {
		t.Errorf("repacked: %v", names)
	}
}

func TestWriteCaseRefusesPathsOutsideAndFullDirectories(t *testing.T) {
	root := t.TempDir()
	for _, path := range []string{"../escape.md", "/abs.md", "a/../../b.md", ""} {
		dir := filepath.Join(root, "out")
		err := writeCase(dir, []*apiv1.CaseFile{{Path: path, Content: []byte("x")}})
		if err == nil || !strings.Contains(err.Error(), "not a path inside") {
			t.Errorf("%q: %v", path, err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "escape.md")); err == nil {
		t.Fatal("a file was written outside the directory")
	}
	// A link is made last, so no file is written through it.
	dir := filepath.Join(root, "linked")
	if err := writeCase(dir, []*apiv1.CaseFile{
		{Path: "a", LinkTarget: ".."},
		{Path: "a/b.md", Content: []byte("x")},
	}); err == nil {
		t.Error("a file under a link's path was written")
	}
	if _, err := os.Stat(filepath.Join(root, "b.md")); err == nil {
		t.Fatal("a file was written through a link")
	}

	full := filepath.Join(root, "full")
	writeFiles(t, full, map[string]string{"keep.md": "mine"})
	err := writeCase(full, []*apiv1.CaseFile{{Path: "case.yaml", Content: []byte("x")}})
	if err == nil || !strings.Contains(err.Error(), "not empty") {
		t.Errorf("a non-empty directory: %v", err)
	}
}

package cli

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
)

func writeFiles(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for name, text := range files {
		path := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

type entry struct {
	name, body string
	mode       int64
	mtime      int64
	uid        int
}

func untar(t *testing.T, data []byte) []entry {
	t.Helper()
	var out []entry
	tr := tar.NewReader(bytes.NewReader(data))
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return out
		}
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(tr)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, entry{h.Name, string(body), h.Mode, h.ModTime.Unix(), h.Uid})
	}
}

func TestPackIsSortedRelativeAndRepeatable(t *testing.T) {
	dir := t.TempDir()
	writeFiles(t, dir, map[string]string{
		"case.yaml":                    "id: x",
		"prompts/b.md":                 "b",
		"prompts/a.md":                 "a",
		"extensions/__pycache__/m.pyc": "junk",
		".hidden":                      "h",
	})
	if err := os.Symlink(filepath.Join(dir, "prompts", "a.md"), filepath.Join(dir, "link.md")); err != nil {
		t.Fatal(err)
	}
	first, err := pack(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(dir, "case.yaml"), timeZero, timeZero); err != nil {
		t.Fatal(err)
	}
	second, err := pack(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("packing the same files twice gave different bytes")
	}
	var names []string
	for _, e := range untar(t, first) {
		names = append(names, e.name)
		if e.mtime != 0 || e.uid != 0 {
			t.Errorf("%s: mtime %d, uid %d; want both zeroed", e.name, e.mtime, e.uid)
		}
		if e.name == "link.md" && e.body != "a" {
			t.Errorf("a symlink packs as %q, want the file it points to", e.body)
		}
	}
	want := []string{".hidden", "case.yaml", "link.md", "prompts/a.md", "prompts/b.md"}
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("packed %v, want %v", names, want)
	}
}

func TestPackRefusesAFileOrAMissingDirectory(t *testing.T) {
	dir := t.TempDir()
	writeFiles(t, dir, map[string]string{"case.yaml": "id: x"})
	if _, err := pack(filepath.Join(dir, "case.yaml")); err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("a file: %v", err)
	}
	if _, err := pack(filepath.Join(dir, "nope")); err == nil {
		t.Error("a missing directory was packed")
	}
}

func TestOverridesAreYAMLFlowSequences(t *testing.T) {
	got, err := parseOverrides([]string{"model=qwen3-8b,glm-5", "n=1,2.5", "paraphrased=[],[dm_ab]", "flag=true"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := protojson.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"flag":[true],"model":["qwen3-8b","glm-5"],"n":[1,2.5],"paraphrased":[[],["dm_ab"]]}`
	if strings.ReplaceAll(string(b), " ", "") != want {
		t.Fatalf("got %s, want %s", b, want)
	}
	for _, bad := range [][]string{{"model"}, {"=a"}, {"model="}, {"m=a", "m=b"}, {"m=[a"}} {
		if _, err := parseOverrides(bad); err == nil {
			t.Errorf("%v was accepted", bad)
		}
	}
}

func TestSuiteBundlesPackEachPathOnceRelativeToTheFile(t *testing.T) {
	root := t.TempDir()
	writeFiles(t, root, map[string]string{
		"cases/a/case.yaml": "id: a",
		"cases/b/case.yaml": "id: b",
		"suites/s.yaml": "schema_version: 1\nid: s\ncases:\n  - path: ../cases/a\n  - path: ../cases/b\n" +
			"  - path: ../cases/a\n    epochs: 1\n",
	})
	text, bundles, err := suiteBundles(filepath.Join(root, "suites", "s.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(text, "schema_version: 1") {
		t.Fatalf("suite text %q", text)
	}
	if len(bundles) != 2 || untar(t, bundles["../cases/b"])[0].body != "id: b" {
		t.Fatalf("bundles %v", bundles)
	}

	writeFiles(t, root, map[string]string{"suites/bad.yaml": "id: s\ncases:\n  - path: ../cases/missing\n"})
	if _, _, err := suiteBundles(filepath.Join(root, "suites", "bad.yaml")); err == nil || !strings.Contains(err.Error(), "cases[0]") {
		t.Fatalf("a missing case directory: %v", err)
	}
}

func TestEditsReadFromYAML(t *testing.T) {
	path := filepath.Join(t.TempDir(), "edits.yaml")
	writeFiles(t, filepath.Dir(path), map[string]string{"edits.yaml": `
- replace_delivery: {send_event_id: e3, recipient: b, content: stay quiet}
- deleteMessage: {agentId: a, index: 4}
`})
	edits, err := readEdits(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(edits) != 2 || edits[0].GetReplaceDelivery().GetContent() != "stay quiet" || edits[1].GetDeleteMessage().GetIndex() != 4 {
		t.Fatalf("edits %v", edits)
	}
	writeFiles(t, filepath.Dir(path), map[string]string{"edits.yaml": "- rewrite: {}\n"})
	if _, err := readEdits(path); err == nil || !strings.Contains(err.Error(), "replace_message") {
		t.Fatalf("an unknown edit: %v", err)
	}
}

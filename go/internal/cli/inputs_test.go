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
	link       string
	typ        byte
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
		out = append(out, entry{h.Name, string(body), h.Linkname, h.Typeflag, h.ModTime.Unix(), h.Uid})
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
	}
	want := []string{".hidden", "case.yaml", "prompts/a.md", "prompts/b.md"}
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("packed %v, want %v", names, want)
	}
}

// Review on #20: a symlink was packed as the file it points to, so a link out of the case
// directory uploaded that file, as a regular member the control plane could not refuse.
func TestPackKeepsSymlinksAsLinks(t *testing.T) {
	root := t.TempDir()
	writeFiles(t, root, map[string]string{"case/case.yaml": "id: x", "case/prompts/a.md": "a", "outside/key": "SECRET"})
	dir := filepath.Join(root, "case")
	for link, target := range map[string]string{
		"inside.md": "prompts/a.md",   // a file in the case: kept as a link
		"leak.md":   "../outside/key", // a file outside: kept as a link, for the control plane to refuse
		"dirlink":   "prompts",        // a directory: left out, as Python's pack leaves it out
		"gone.md":   "nope.md",        // nowhere: left out
	} {
		if err := os.Symlink(target, filepath.Join(dir, link)); err != nil {
			t.Fatal(err)
		}
	}
	data, err := pack(dir)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte("SECRET")) {
		t.Fatal("the bundle holds the content of a file outside the case directory")
	}
	got := map[string]string{}
	for _, e := range untar(t, data) {
		if e.typ == tar.TypeSymlink {
			got[e.name] = "-> " + e.link
		} else {
			got[e.name] = e.body
		}
	}
	want := map[string]string{
		"case.yaml": "id: x", "prompts/a.md": "a", "inside.md": "-> prompts/a.md", "leak.md": "-> ../outside/key",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("packed %v, want %v", got, want)
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
	got, err := parseOverrides([]string{"framing=neutral,pressure", "n=1,2.5", "paraphrased=[],[dm_ab]", "flag=true"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := protojson.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"flag":[true],"framing":["neutral","pressure"],"n":[1,2.5],"paraphrased":[[],["dm_ab"]]}`
	if strings.ReplaceAll(string(b), " ", "") != want {
		t.Fatalf("got %s, want %s", b, want)
	}
	for _, bad := range [][]string{{"framing"}, {"=a"}, {"framing="}, {"m=a", "m=b"}, {"m=[a"}} {
		if _, err := parseOverrides(bad); err == nil {
			t.Errorf("%v was accepted", bad)
		}
	}
}

func TestModelsFillTheDefaultSlotOrANamedOne(t *testing.T) {
	got, err := parseModels([]string{"qwen3-8b,glm-5", "attacker=org/model-x"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || strings.Join(got["default"].GetNames(), ",") != "qwen3-8b,glm-5" || strings.Join(got["attacker"].GetNames(), ",") != "org/model-x" {
		t.Fatalf("got %v", got)
	}
	for _, bad := range [][]string{{""}, {"=m"}, {"a="}, {"m1,,m2"}, {"m1", "default=m2"}} {
		if _, err := parseModels(bad); err == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}

func TestReplayModelsAreOneModelPerSlot(t *testing.T) {
	edits, err := modelEdits([]string{"glm-5", "attacker=m2"})
	if err != nil {
		t.Fatal(err)
	}
	first, second := edits[0].GetReplaceModel(), edits[1].GetReplaceModel()
	if len(edits) != 2 || first.GetSlot() != "default" || first.GetModel() != "glm-5" || second.GetSlot() != "attacker" || second.GetModel() != "m2" {
		t.Fatalf("edits %v", edits)
	}
	for _, bad := range [][]string{{"a,b"}, {"x="}, {"m1", "m2"}} {
		if _, err := modelEdits(bad); err == nil {
			t.Errorf("%q was accepted", bad)
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

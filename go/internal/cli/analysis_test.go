package cli

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// fakeChunks is a download of the given chunks that then ends with err.
type fakeChunks struct {
	chunks []string
	err    error
	at     int
}

func (f *fakeChunks) Receive() bool {
	f.at++
	return f.at <= len(f.chunks)
}

func (f *fakeChunks) Msg() *apiv1.DownloadExportResponse {
	return &apiv1.DownloadExportResponse{Chunk: []byte(f.chunks[f.at-1])}
}

func (f *fakeChunks) Err() error { return f.err }

// failingWriter takes its first write and fails the next, as a full disk does.
type failingWriter struct{ writes int }

func (w *failingWriter) Write(p []byte) (int, error) {
	w.writes++
	if w.writes > 1 {
		return 0, errors.New("no space left on device")
	}
	return len(p), nil
}

func TestADownloadIsWrittenWholeOrNotAtAll(t *testing.T) {
	dir := t.TempDir()
	done := filepath.Join(dir, "done.eval")
	n, err := download(&fakeChunks{chunks: []string{"first-", "second"}}, nil, done)
	if got, _ := os.ReadFile(done); err != nil || n != 12 || string(got) != "first-second" {
		t.Fatalf("download: %d %v %q", n, err, got)
	}

	// The stream breaks after a chunk was written: no file stays.
	broken := filepath.Join(dir, "broken.eval")
	_, err = download(&fakeChunks{chunks: []string{"first-"}, err: errors.New("connection reset")}, nil, broken)
	if err == nil {
		t.Fatal("a broken stream was reported as a download")
	}
	if _, statErr := os.Stat(broken); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("a partial file stayed: %v", statErr)
	}

	// A refused download makes no file at all.
	refused := filepath.Join(dir, "refused.eval")
	if _, err := download(&fakeChunks{err: errors.New("not found")}, nil, refused); err == nil {
		t.Fatal("a refused download was reported as one")
	}
	if _, statErr := os.Stat(refused); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("a refused download left a file: %v", statErr)
	}

	// An existing file is neither overwritten nor removed.
	_, err = download(&fakeChunks{chunks: []string{"other"}}, nil, done)
	if got, _ := os.ReadFile(done); err == nil || !strings.Contains(err.Error(), "not overwritten") || string(got) != "first-second" {
		t.Fatalf("an existing file: %v %q", err, got)
	}

	// A write that fails is an error, on stdout too.
	var out failingWriter
	if _, err := download(&fakeChunks{chunks: []string{"a", "b"}}, &out, "-"); err == nil || !strings.Contains(err.Error(), "no space left") {
		t.Fatalf("a failed write: %v", err)
	}
	var whole bytes.Buffer
	if n, err := download(&fakeChunks{chunks: []string{"a", "b"}}, &whole, "-"); err != nil || n != 2 || whole.String() != "ab" {
		t.Fatalf("stdout: %d %v %q", n, err, whole.String())
	}
}

func TestRepeatedColumnLabelsStayDistinctAsJSONKeys(t *testing.T) {
	for _, c := range []struct{ in, want []string }{
		{[]string{"run_id", "status"}, []string{"run_id", "status"}},
		{[]string{"run_id", "run_id", "seq", "run_id"}, []string{"run_id", "run_id_2", "seq", "run_id_3"}},
		{[]string{"a", "a", "a_2"}, []string{"a", "a_3", "a_2"}},
		{nil, []string{}},
	} {
		if got := uniqueNames(c.in); !reflect.DeepEqual(got, c.want) {
			t.Errorf("uniqueNames(%v) = %v, want %v", c.in, got, c.want)
		}
	}
}

func TestCellRendersQueryValues(t *testing.T) {
	for _, c := range []struct {
		value any
		want  string
	}{
		{nil, "NULL"},
		{"plain text", "plain text"},
		{true, "true"},
		{6.0, "6"},
		{-3.0, "-3"},
		{0.25, "0.25"},
		{1e300, "1e+300"},
		{[]any{"a", 1.0}, `["a",1]`},
		{map[string]any{"k": nil}, `{"k":null}`},
	} {
		if got := cell(c.value, "NULL"); got != c.want {
			t.Errorf("cell(%v) = %q, want %q", c.value, got, c.want)
		}
	}
	if got := oneLine("a\nb\tc"); got != `a\nb\tc` {
		t.Errorf("oneLine: %q", got)
	}
}

package sandboxd

import (
	"bytes"
	"testing"
)

func TestPidWriterStripsThePidLineAcrossWrites(t *testing.T) {
	var out bytes.Buffer
	w := &pidWriter{next: &out}

	for _, chunk := range []string{"12", "34\nerr", "or\n"} {
		if _, err := w.Write([]byte(chunk)); err != nil {
			t.Fatal(err)
		}
	}

	if w.pid != "1234" || out.String() != "error\n" {
		t.Fatalf("pid = %q, forwarded = %q", w.pid, out.String())
	}
}

func TestPidWriterForwardsAFirstLineThatIsNotAPid(t *testing.T) {
	var out bytes.Buffer
	w := &pidWriter{next: &out}

	if _, err := w.Write([]byte("OCI runtime exec failed\nmore")); err != nil {
		t.Fatal(err)
	}

	if w.pid != "" || out.String() != "OCI runtime exec failed\nmore" {
		t.Fatalf("pid = %q, forwarded = %q", w.pid, out.String())
	}
}

func TestPidWriterFlushForwardsAnUnfinishedLine(t *testing.T) {
	var out bytes.Buffer
	w := &pidWriter{next: &out}
	if _, err := w.Write([]byte("no newline")); err != nil {
		t.Fatal(err)
	}

	if err := w.flush(); err != nil {
		t.Fatal(err)
	}

	if out.String() != "no newline" {
		t.Fatalf("forwarded = %q", out.String())
	}
}

func TestShortOutputIsInline(t *testing.T) {
	c := &capBuffer{limit: 100}
	_, _ = c.Write([]byte("hello"))

	out, blob := c.output(10)

	if string(out.Inline) != "hello" || out.Size != 5 || out.BlobSHA256 != "" || blob != nil {
		t.Fatalf("output = %+v, blob = %v", out, blob)
	}
}

func TestLongOutputIsInlinePrefixPlusBlob(t *testing.T) {
	c := &capBuffer{limit: 100}
	_, _ = c.Write([]byte("0123456789abcdef"))

	out, blob := c.output(4)

	if string(out.Inline) != "0123" || out.Size != 16 || out.Capped {
		t.Fatalf("output = %+v", out)
	}
	if blob == nil || string(blob.Data) != "0123456789abcdef" || blob.SHA256 != out.BlobSHA256 {
		t.Fatalf("blob = %+v", blob)
	}
}

func TestOutputOverTheLimitIsCapped(t *testing.T) {
	c := &capBuffer{limit: 8}
	_, _ = c.Write([]byte("0123456789"))
	_, _ = c.Write([]byte("abcdef"))

	out, blob := c.output(100)

	if out.Size != 16 || !out.Capped || blob == nil || string(blob.Data) != "01234567" {
		t.Fatalf("output = %+v, blob = %+v", out, blob)
	}
}

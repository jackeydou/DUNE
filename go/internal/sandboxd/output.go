package sandboxd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"strconv"
)

// wrapArgv prints the shell's pid to stderr, then execs the command in the same process, so
// the first stderr line is the command's pid inside the sandbox. Timeouts need that pid:
// exec'd processes do not lead their own process group, so there is no group to kill.
func wrapArgv(argv []string) []string {
	return append([]string{"/bin/sh", "-c", `echo $$ >&2; exec "$@"`, "sh"}, argv...)
}

// killTreeScript stops the process given as $1 and every descendant, then kills them all.
// Stopping first keeps the tree from growing while it is walked. POSIX sh builtins only, so
// it runs on busybox. A process that double-forked has left the tree and survives; it is
// then reported as a surviving process.
const killTreeScript = `root=$1
kill -STOP "$root" 2>/dev/null
tree=" $root "
grew=1
while [ "$grew" = 1 ]; do
  grew=0
  for d in /proc/[0-9]*; do
    p=${d#/proc/}
    case "$tree" in *" $p "*) continue ;; esac
    { read -r stat < "$d/stat"; } 2>/dev/null || continue
    set -- ${stat##*) }
    case "$tree" in *" $2 "*) tree="$tree$p "; kill -STOP "$p" 2>/dev/null; grew=1 ;; esac
  done
done
kill -KILL $tree 2>/dev/null
exit 0`

// pidWriter takes the pid line wrapArgv prints off the front of stderr and forwards the rest.
// A first line that is not a pid is forwarded untouched.
type pidWriter struct {
	next io.Writer
	line []byte
	done bool
	pid  string
}

func (w *pidWriter) Write(p []byte) (int, error) {
	n := len(p)
	if !w.done {
		i := bytes.IndexByte(p, '\n')
		if i < 0 {
			w.line = append(w.line, p...)
			return n, nil
		}
		w.line = append(w.line, p[:i]...)
		w.done = true
		if _, err := strconv.ParseUint(string(w.line), 10, 32); err == nil {
			w.pid = string(w.line)
		} else if _, err := w.next.Write(append(w.line, '\n')); err != nil {
			return 0, err
		}
		p = p[i+1:]
	}
	if _, err := w.next.Write(p); err != nil {
		return 0, err
	}
	return n, nil
}

// flush forwards a partial first line that never ended, for a command that wrote to stderr
// without the wrapper's pid line in front.
func (w *pidWriter) flush() error {
	if w.done || len(w.line) == 0 {
		return nil
	}
	w.done = true
	_, err := w.next.Write(w.line)
	return err
}

// capBuffer keeps the first limit bytes written and counts the rest.
type capBuffer struct {
	limit int
	buf   []byte
	size  int64
}

func (c *capBuffer) Write(p []byte) (int, error) {
	c.size += int64(len(p))
	if room := c.limit - len(c.buf); room > 0 {
		c.buf = append(c.buf, p[:min(room, len(p))]...)
	}
	return len(p), nil
}

// Output is a command's stdout or stderr.
type Output struct {
	Inline     []byte
	Size       int64
	BlobSHA256 string
	Capped     bool
}

// Blob is content that follows a header, identified by its sha256.
type Blob struct {
	SHA256 string
	Data   []byte
}

func (c *capBuffer) output(inlineLimit int) (Output, *Blob) {
	out := Output{Size: c.size, Capped: c.size > int64(len(c.buf))}
	if len(c.buf) <= inlineLimit && !out.Capped {
		out.Inline = c.buf
		return out, nil
	}
	out.Inline = c.buf[:min(inlineLimit, len(c.buf))]
	sum := sha256.Sum256(c.buf)
	out.BlobSHA256 = hex.EncodeToString(sum[:])
	return out, &Blob{SHA256: out.BlobSHA256, Data: c.buf}
}

package sandboxd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

const (
	// DisplayUser runs a display image's display stack and the tools that drive it. Display
	// images must have it.
	DisplayUser = "swarmdisplay"
	// DisplayDir holds the display stack's state, outside every key path.
	DisplayDir = "/run/swarm-display"
)

// Display is a sandbox's virtual screen.
type Display struct {
	Width  uint32
	Height uint32
	// URL is the page the browser opens first; empty opens about:blank.
	URL string
}

func checkDisplay(d *Display, mounts []Mount) error {
	if d == nil {
		return nil
	}
	if d.Width < 320 || d.Width > 1920 || d.Height < 240 || d.Height > 1200 {
		return fmt.Errorf("%w: display %dx%d is outside 320x240 to 1920x1200", ErrInvalid, d.Width, d.Height)
	}
	if len(d.URL) > 2048 || strings.ContainsAny(d.URL, "\x00\n\r") {
		return fmt.Errorf("%w: display url %.80q is over 2048 bytes or holds NUL or a line break", ErrInvalid, d.URL)
	}
	for _, m := range mounts {
		if overlaps(m.Path, DisplayDir) {
			return fmt.Errorf("%w: key path %s overlaps the display's state directory %s, which must stay out of every key path", ErrInvalid, m.Path, DisplayDir)
		}
	}
	return nil
}

// startDisplay runs the image's `swarm-display start`, which returns once the display stack
// is up and prints the stack's pid. It returns that process's uid, whose processes are the
// display's from then on.
func (s *Service) startDisplay(ctx context.Context, sb *sandbox, d *Display) (string, error) {
	argv := []string{
		"swarm-display", "start",
		"--width", strconv.FormatUint(uint64(d.Width), 10),
		"--height", strconv.FormatUint(uint64(d.Height), 10),
	}
	if d.URL != "" {
		argv = append(argv, "--url", d.URL)
	}
	startCtx, cancel := context.WithTimeout(ctx, s.cfg.DisplayStartTimeout)
	defer cancel()
	stdout := &capBuffer{limit: 4 << 10}
	stderr := &capBuffer{limit: 16 << 10}
	code, err := s.drv.Exec(startCtx, sb.container, driver.ExecSpec{Argv: argv, User: DisplayUser}, stdout, stderr)
	if err != nil {
		return "", fmt.Errorf("start the display in container %s (timeout %s): %w", sb.container, s.cfg.DisplayStartTimeout, err)
	}
	if code != 0 {
		return "", fmt.Errorf("start the display in container %s: `swarm-display start` exited %d: %s. The image must be built from swarmeval/display (deploy/images/display)",
			sb.container, code, strings.TrimSpace(string(stderr.buf)))
	}
	pid, err := strconv.ParseInt(strings.TrimSpace(string(stdout.buf)), 10, 32)
	if err != nil {
		return "", fmt.Errorf("start the display in container %s: `swarm-display start` printed %.80q, not the display stack's pid", sb.container, stdout.buf)
	}
	procs, err := s.processes(ctx, sb)
	if err != nil {
		return "", err
	}
	for _, p := range procs {
		if p.PID == int32(pid) {
			return p.UID, nil
		}
	}
	return "", fmt.Errorf("start the display in container %s: its pid %d is not running after `swarm-display start` returned", sb.container, pid)
}

// collectScript prints the regular file $1 and removes it; exit 4 when there is none. A
// symlink is removed without being followed.
const collectScript = `f=$1
if [ -f "$f" ] && [ ! -h "$f" ]; then cat -- "$f" || exit 5; rm -f -- "$f"; exit 0; fi
rm -f -- "$f" 2>/dev/null
exit 4`

// Collected is one file an Exec collected.
type Collected struct {
	Path    string
	Missing bool
	Size    int64
	// SHA256 is empty when the file is over the collect limit; otherwise its content is
	// among the result's blobs.
	SHA256 string
}

func checkCollect(paths []string, mounts []Mount) error {
	for _, p := range paths {
		if !path.IsAbs(p) || path.Clean(p) != p || strings.ContainsRune(p, 0) {
			return fmt.Errorf("%w: collect path %q must be absolute and clean", ErrInvalid, p)
		}
		for _, m := range mounts {
			if overlaps(p, m.Path) {
				return fmt.Errorf("%w: collect path %s overlaps key path %s; collected files live outside key paths", ErrInvalid, p, m.Path)
			}
		}
	}
	return nil
}

// collect reads and removes each path, in order, as the call's user: root has no capability to
// read another user's private directory, and what a call may collect is what its user may read.
func (s *Service) collect(ctx context.Context, sb *sandbox, user string, paths []string, blobs *[]Blob) ([]Collected, error) {
	out := make([]Collected, 0, len(paths))
	for _, p := range paths {
		readCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), s.cfg.HelperTimeout)
		stdout := &capBuffer{limit: int(s.cfg.CollectLimit)}
		stderr := &capBuffer{limit: 4 << 10}
		argv := []string{"/bin/sh", "-c", collectScript, "sh", p}
		code, err := s.drv.Exec(readCtx, sb.container, driver.ExecSpec{Argv: argv, User: user}, stdout, stderr)
		cancel()
		if err != nil {
			return nil, fmt.Errorf("collect %s from container %s: %w", p, sb.container, err)
		}
		switch code {
		case 0:
		case 4:
			out = append(out, Collected{Path: p, Missing: true})
			continue
		default:
			return nil, fmt.Errorf("collect %s from container %s: exit %d: %s", p, sb.container, code, strings.TrimSpace(string(stderr.buf)))
		}
		c := Collected{Path: p, Size: stdout.size}
		if stdout.size <= s.cfg.CollectLimit {
			sum := sha256.Sum256(stdout.buf)
			c.SHA256 = hex.EncodeToString(sum[:])
			*blobs = append(*blobs, Blob{SHA256: c.SHA256, Data: stdout.buf})
		}
		out = append(out, c)
	}
	return out, nil
}

// overlaps tells whether one of two clean absolute paths is the other or under it.
func overlaps(a, b string) bool {
	return a == "/" || b == "/" || under(a, b) || under(b, a)
}

// withoutDisplay drops the display user's processes: they belong to the display, not to a call.
func (sb *sandbox) withoutDisplay(procs []driver.Process) []driver.Process {
	if sb.displayUID == "" {
		return procs
	}
	kept := procs[:0:0]
	for _, p := range procs {
		if p.UID != sb.displayUID {
			kept = append(kept, p)
		}
	}
	return kept
}

// defaultDisplayStartTimeout bounds `swarm-display start`, which waits for the browser.
const defaultDisplayStartTimeout = 60 * time.Second

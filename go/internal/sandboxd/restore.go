package sandboxd

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

// ErrState is a request the sandbox's state no longer allows.
var ErrState = errors.New("failed precondition")

// RestoreDir is a directory to create in a key path.
type RestoreDir struct {
	Path string
	Mode fs.FileMode
}

// RestoreRequest puts a sandbox's key paths into a state an earlier run recorded, for a fork
// (M2 spec decision 9): paths to remove, then directories, then files.
type RestoreRequest struct {
	RunID     string
	SandboxID string
	Remove    []string
	Dirs      []RestoreDir
	Files     []SeedFile
}

// RestoreFiles applies req before the sandbox's first command and takes the manifest again,
// so what it wrote is the baseline and never reported as a change. A path to remove that does
// not exist is skipped. It may be called several times, to keep each request small.
func (s *Service) RestoreFiles(_ context.Context, req RestoreRequest) error {
	sb, err := s.lookup(req.RunID, req.SandboxID)
	if err != nil {
		return err
	}
	sb.mu.Lock()
	defer sb.mu.Unlock()
	if sb.execd {
		return fmt.Errorf("%w: sandbox %s of run %s has already run a command; files are restored only before the first", ErrState, req.SandboxID, req.RunID)
	}
	paths := slices.Clone(req.Remove)
	for _, d := range req.Dirs {
		paths = append(paths, d.Path)
	}
	for _, p := range paths {
		if err := checkInside(p, sb.mounts); err != nil {
			return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
		}
	}
	if err := checkSeeds(req.Files, sb.mounts, s.cfg.RestoreLimit); err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	fsDir := filepath.Join(s.cfg.StateDir, req.RunID, req.SandboxID, "fs")
	remove := slices.Clone(req.Remove)
	// Deepest first, so a directory's contents go before it.
	sort.Slice(remove, func(i, j int) bool { return strings.Count(remove[i], "/") > strings.Count(remove[j], "/") })
	for _, p := range remove {
		if err := within(sb.mounts, fsDir, p, func(root *os.Root, name string) error {
			err := root.RemoveAll(name)
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return err
		}); err != nil {
			return fmt.Errorf("remove %s in sandbox %s: %w", p, req.SandboxID, err)
		}
	}
	dirs := slices.Clone(req.Dirs)
	sort.Slice(dirs, func(i, j int) bool { return strings.Count(dirs[i].Path, "/") < strings.Count(dirs[j].Path, "/") })
	for _, d := range dirs {
		mode := d.Mode.Perm()
		if mode == 0 {
			mode = 0o755
		}
		if err := within(sb.mounts, fsDir, d.Path, func(root *os.Root, name string) error {
			if err := root.MkdirAll(name, mode); err != nil {
				return err
			}
			return root.Chmod(name, mode)
		}); err != nil {
			return fmt.Errorf("create directory %s in sandbox %s: %w", d.Path, req.SandboxID, err)
		}
	}
	if err := seed(sb.mounts, fsDir, req.Files); err != nil {
		return fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
	}
	manifest, err := fsdiff.Walk(sb.roots, nil)
	if err != nil {
		return err
	}
	sb.manifest = manifest
	return nil
}

// checkInside requires p to be absolute, clean, and strictly inside a key path.
func checkInside(p string, mounts []Mount) error {
	if !path.IsAbs(p) || path.Clean(p) != p {
		return fmt.Errorf("%w: path %q must be absolute and clean", ErrInvalid, p)
	}
	if !slices.ContainsFunc(mounts, func(m Mount) bool { return strings.HasPrefix(p, m.Path+"/") }) {
		return fmt.Errorf("%w: path %s is not strictly inside a key path", ErrInvalid, p)
	}
	return nil
}

// within runs op on p's name relative to the host directory of its top-level key path,
// through os.Root, so nothing outside the key path is touched.
func within(mounts []Mount, fsDir, p string, op func(root *os.Root, name string) error) error {
	top := p
	for parent := parentMount(mounts, top); parent != ""; parent = parentMount(mounts, top) {
		top = parent
	}
	root, err := os.OpenRoot(filepath.Join(fsDir, filepath.FromSlash(top)))
	if err != nil {
		return err
	}
	defer func() { _ = root.Close() }() // op's result is what matters
	return op(root, strings.TrimPrefix(p, top+"/"))
}

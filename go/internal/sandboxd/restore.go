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
	"syscall"

	"github.com/jackeydou/DUNE/go/internal/fsdiff"
)

// ErrState is a request the sandbox's state no longer allows.
var ErrState = errors.New("failed precondition")

// RestoreDir is a directory to create in a key path.
type RestoreDir struct {
	Path string
	Mode fs.FileMode
	UID  uint32
}

// RestoreFile is a regular file to write in a key path.
type RestoreFile struct {
	Path    string
	Content []byte
	Mode    fs.FileMode
	UID     uint32
}

// RestoreRequest puts a sandbox's key paths into a state an earlier run recorded, for a fork
// (M2 spec decision 9): paths to remove, then directories, then files.
type RestoreRequest struct {
	RunID     string
	SandboxID string
	Remove    []string
	Dirs      []RestoreDir
	Files     []RestoreFile
}

// RestoreFiles applies req before the sandbox's first command and takes the manifest again,
// so what it wrote is the baseline and never reported as a change. A path to remove that does
// not exist is skipped. Whatever is at a directory's or file's path and is of another kind is
// removed first, so a write never follows a symlink. Owners are set where they differ from the
// one sandboxd writes as; it returns the paths it could not give their owner because it is not
// root. It may be called several times, to keep each request small.
func (s *Service) RestoreFiles(_ context.Context, req RestoreRequest) ([]string, error) {
	sb, err := s.lookup(req.RunID, req.SandboxID)
	if err != nil {
		return nil, err
	}
	sb.mu.Lock()
	defer sb.mu.Unlock()
	if sb.execd {
		return nil, fmt.Errorf("%w: sandbox %s of run %s has already run a command; files are restored only before the first", ErrState, req.SandboxID, req.RunID)
	}
	paths := slices.Clone(req.Remove)
	for _, d := range req.Dirs {
		paths = append(paths, d.Path)
	}
	total := 0
	for _, f := range req.Files {
		paths = append(paths, f.Path)
		total += len(f.Content)
	}
	for _, p := range paths {
		if err := checkInside(p, sb.mounts); err != nil {
			return nil, fmt.Errorf("sandbox %s of run %s: %w", req.SandboxID, req.RunID, err)
		}
	}
	if total > s.cfg.RestoreLimit {
		return nil, fmt.Errorf("%w: sandbox %s of run %s: restored files hold %d bytes, over the %d byte limit per request", ErrInvalid, req.SandboxID, req.RunID, total, s.cfg.RestoreLimit)
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
			return nil, fmt.Errorf("remove %s in sandbox %s: %w", p, req.SandboxID, err)
		}
	}
	var unowned []string
	dirs := slices.Clone(req.Dirs)
	sort.Slice(dirs, func(i, j int) bool { return strings.Count(dirs[i].Path, "/") < strings.Count(dirs[j].Path, "/") })
	for _, d := range dirs {
		mode := d.Mode.Perm()
		if mode == 0 {
			mode = 0o755
		}
		owned := true
		if err := within(sb.mounts, fsDir, d.Path, func(root *os.Root, name string) error {
			if err := clearOther(root, name, fs.ModeDir); err != nil {
				return err
			}
			if err := root.MkdirAll(name, mode); err != nil {
				return err
			}
			if err := root.Chmod(name, mode); err != nil {
				return err
			}
			owned, err = own(root, name, d.UID)
			return err
		}); err != nil {
			return nil, fmt.Errorf("create directory %s in sandbox %s: %w", d.Path, req.SandboxID, err)
		}
		if !owned {
			unowned = append(unowned, d.Path)
		}
	}
	for _, f := range req.Files {
		owned := true
		if err := within(sb.mounts, fsDir, f.Path, func(root *os.Root, name string) error {
			if err := clearOther(root, name, 0); err != nil {
				return err
			}
			if err := writeFile(root, name, f); err != nil {
				return err
			}
			owned, err = own(root, name, f.UID)
			return err
		}); err != nil {
			return nil, fmt.Errorf("restore file %s in sandbox %s: %w", f.Path, req.SandboxID, err)
		}
		if !owned {
			unowned = append(unowned, f.Path)
		}
	}
	manifest, err := fsdiff.Walk(sb.roots, nil)
	if err != nil {
		return nil, err
	}
	sb.manifest = manifest
	return unowned, nil
}

// clearOther removes what is at name unless it is of kind (fs.ModeDir, or 0 for a regular
// file). Lstat does not follow a symlink, so a link is always removed rather than written
// through.
func clearOther(root *os.Root, name string, kind fs.FileMode) error {
	info, err := root.Lstat(name)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode().Type() == kind {
		return nil
	}
	return root.RemoveAll(name)
}

func writeFile(root *os.Root, name string, f RestoreFile) error {
	mode := f.Mode.Perm()
	if mode == 0 {
		mode = 0o644
	}
	if err := root.MkdirAll(path.Dir(name), 0o755); err != nil {
		return err
	}
	file, err := root.OpenFile(name, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(f.Content)
	if err := errors.Join(writeErr, file.Close()); err != nil {
		return err
	}
	return root.Chmod(name, mode)
}

// own gives name owner uid if it has another. It reports false, and no error, when sandboxd
// may not change owners because it is not root.
func own(root *os.Root, name string, uid uint32) (bool, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return false, err
	}
	if st, ok := info.Sys().(*syscall.Stat_t); ok && st.Uid == uid {
		return true, nil
	}
	err = root.Lchown(name, int(uid), -1)
	if errors.Is(err, fs.ErrPermission) || errors.Is(err, syscall.EPERM) {
		return false, nil
	}
	return err == nil, err
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

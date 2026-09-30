// Package fsdiff builds manifests of a sandbox's key paths from the host side and diffs them.
//
// A manifest records metadata for every entry and a sha256 for files and symlinks. An entry
// whose kind, size, mtime, and inode match the previous manifest keeps its previous hash
// without being read again, so a write that preserves all four goes unseen. Directory mtime
// and size are ignored: they change with every child and are reported through the children.
package fsdiff

import (
	"cmp"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"syscall"
)

// Kind of a manifest entry.
type Kind int

const (
	KindFile Kind = iota + 1
	KindDir
	KindSymlink
	// KindOther covers fifos, sockets, and devices. They are never opened.
	KindOther
)

// Entry is one file, directory, symlink, or special file.
type Entry struct {
	Kind    Kind
	Size    int64
	MtimeNs int64
	Inode   uint64
	Mode    fs.FileMode
	UID     uint32
	// SHA256 of a file's content or a symlink's target, hex. Empty for other kinds.
	SHA256 string
	// HostPath is where sandboxd reads the entry. Not compared.
	HostPath string
}

// Manifest maps paths inside the sandbox to entries.
type Manifest map[string]Entry

// Root maps a host directory to the path where the sandbox sees it.
type Root struct {
	HostDir       string
	ContainerPath string
}

// Walk builds the manifest of roots, reusing hashes from prev where metadata is unchanged.
// Entries that vanish mid-walk are skipped: a background process may be deleting them.
func Walk(roots []Root, prev Manifest) (Manifest, error) {
	out := Manifest{}
	for _, root := range roots {
		err := filepath.WalkDir(root.HostDir, func(host string, d fs.DirEntry, err error) error {
			if err != nil {
				if errors.Is(err, fs.ErrNotExist) {
					return nil
				}
				return err
			}
			if host == root.HostDir {
				return nil
			}
			rel, err := filepath.Rel(root.HostDir, host)
			if err != nil {
				return err
			}
			name := path.Join(root.ContainerPath, filepath.ToSlash(rel))
			entry, err := stat(host, d, prev[name])
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			if err != nil {
				return err
			}
			out[name] = entry
			return nil
		})
		if err != nil {
			return nil, fmt.Errorf("walk %s (sandbox path %s): %w", root.HostDir, root.ContainerPath, err)
		}
	}
	return out, nil
}

func stat(host string, d fs.DirEntry, prev Entry) (Entry, error) {
	info, err := d.Info()
	if err != nil {
		return Entry{}, err
	}
	sys, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return Entry{}, fmt.Errorf("stat %s: no unix metadata on this platform", host)
	}
	e := Entry{
		Size:     info.Size(),
		MtimeNs:  info.ModTime().UnixNano(),
		Inode:    sys.Ino,
		Mode:     info.Mode(),
		UID:      sys.Uid,
		HostPath: host,
	}
	switch {
	case info.Mode().IsRegular():
		e.Kind = KindFile
	case info.IsDir():
		e.Kind = KindDir
		return e, nil
	case info.Mode()&fs.ModeSymlink != 0:
		e.Kind = KindSymlink
	default:
		e.Kind = KindOther
		return e, nil
	}
	if prev.SHA256 != "" && prev.Kind == e.Kind && prev.Size == e.Size &&
		prev.MtimeNs == e.MtimeNs && prev.Inode == e.Inode {
		e.SHA256 = prev.SHA256
		return e, nil
	}
	if e.SHA256, err = hashEntry(host, e.Kind); err != nil {
		return Entry{}, err
	}
	return e, nil
}

func hashEntry(host string, kind Kind) (string, error) {
	if kind == KindSymlink {
		target, err := os.Readlink(host)
		if err != nil {
			return "", err
		}
		sum := sha256.Sum256([]byte(target))
		return hex.EncodeToString(sum[:]), nil
	}
	f, err := os.Open(host)
	if err != nil {
		return "", err
	}
	defer func() { _ = f.Close() }() // read-only: a close error carries nothing
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// Op is what happened to a path between two manifests.
type Op int

const (
	OpCreate Op = iota + 1
	OpModify
	OpDelete
)

// Change is one path that differs. Before is zero for a create, After for a delete.
type Change struct {
	Path   string
	Op     Op
	Before Entry
	After  Entry
}

// Diff lists the paths that differ between two manifests, sorted by path.
func Diff(before, after Manifest) []Change {
	var out []Change
	for name, b := range before {
		a, ok := after[name]
		switch {
		case !ok:
			out = append(out, Change{Path: name, Op: OpDelete, Before: b})
		case changed(b, a):
			out = append(out, Change{Path: name, Op: OpModify, Before: b, After: a})
		}
	}
	for name, a := range after {
		if _, ok := before[name]; !ok {
			out = append(out, Change{Path: name, Op: OpCreate, After: a})
		}
	}
	slices.SortFunc(out, func(x, y Change) int { return cmp.Compare(x.Path, y.Path) })
	return out
}

func changed(b, a Entry) bool {
	if b.Kind != a.Kind || b.Mode != a.Mode || b.UID != a.UID {
		return true
	}
	if a.Kind == KindDir {
		return false
	}
	return b.Size != a.Size || b.MtimeNs != a.MtimeNs || b.Inode != a.Inode || b.SHA256 != a.SHA256
}

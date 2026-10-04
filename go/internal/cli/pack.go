package cli

import (
	"archive/tar"
	"bytes"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// maxBundleBytes is edge's limit on a case bundle, and on a suite's bundles in all.
const maxBundleBytes = 64 << 20

// pack archives a case directory as edge expects it: every regular file (a symlink counts as
// the file it points to), relative to dir, in sorted order, with `__pycache__` left out, and with
// times and owners zeroed so the same files always make the same bytes. This is what
// swarmeval.control.bundles.pack does.
func pack(dir string) ([]byte, error) {
	info, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("case directory %s: %w", dir, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("case %s is not a directory; give the directory that holds case.yaml", dir)
	}
	var files []string
	err = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && d.Name() == "__pycache__" {
			return filepath.SkipDir
		}
		if d.IsDir() {
			return nil
		}
		target, err := os.Stat(path)
		if err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		if target.Mode().IsRegular() {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("read case directory %s: %w", dir, err)
	}
	rel := make(map[string]string, len(files))
	for _, f := range files {
		r, err := filepath.Rel(dir, f)
		if err != nil {
			return nil, err
		}
		rel[f] = filepath.ToSlash(r)
	}
	sort.Slice(files, func(i, j int) bool { return rel[files[i]] < rel[files[j]] })

	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, f := range files {
		if err := addFile(tw, f, rel[f]); err != nil {
			return nil, err
		}
		if buf.Len() > maxBundleBytes {
			return nil, fmt.Errorf("case %s is over %d bytes (64 MiB) packed; move large data out of the case directory", dir, maxBundleBytes)
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func addFile(tw *tar.Writer, path, name string) error {
	src, err := os.Open(path)
	if err != nil {
		return err
	}
	defer func() { _ = src.Close() }() // read-only; a close error loses nothing
	info, err := src.Stat()
	if err != nil {
		return err
	}
	if err := tw.WriteHeader(&tar.Header{
		Typeflag: tar.TypeReg,
		Name:     name,
		Mode:     int64(info.Mode().Perm()),
		Size:     info.Size(),
		ModTime:  time.Unix(0, 0),
	}); err != nil {
		return fmt.Errorf("pack %s: %w", path, err)
	}
	if _, err := io.Copy(tw, src); err != nil {
		return fmt.Errorf("pack %s: %w", path, err)
	}
	return nil
}

// isSuiteFile tells a suite file from a case directory for `swarm run`.
func isSuiteFile(path string) (bool, error) {
	info, err := os.Stat(path)
	if err != nil {
		return false, fmt.Errorf("%s: %w", path, err)
	}
	if info.IsDir() {
		return false, nil
	}
	if ext := strings.ToLower(filepath.Ext(path)); ext != ".yaml" && ext != ".yml" {
		return false, fmt.Errorf("%s is neither a case directory nor a suite file (.yaml)", path)
	}
	return true, nil
}

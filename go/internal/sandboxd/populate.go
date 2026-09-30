package sandboxd

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"strings"
)

// extract writes a tar stream from the image into dir, dropping the first path component
// (docker roots the archive at the copied path's base name). All writes go through os.Root,
// so no entry, link, or symlink can reach outside dir. Ownership is kept only when sandboxd
// runs as root; otherwise files belong to sandboxd's user.
//
// Special files (devices, fifos) are skipped: key paths hold workspaces, not devices.
func extract(r io.Reader, dir string) error {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return err
	}
	defer func() { _ = root.Close() }() // file writes are checked where they happen
	chown := os.Geteuid() == 0

	tr := tar.NewReader(r)
	for {
		header, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read image archive: %w", err)
		}
		name := stripFirst(header.Name)
		if name == "" {
			continue
		}
		perm := os.FileMode(header.Mode).Perm()
		if err := root.MkdirAll(path.Dir(name), 0o755); err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := root.MkdirAll(name, perm); err != nil {
				return err
			}
		case tar.TypeReg:
			f, err := root.OpenFile(name, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, perm)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(f, tr)
			if err := errors.Join(copyErr, f.Close()); err != nil {
				return fmt.Errorf("write %s: %w", name, err)
			}
		case tar.TypeSymlink:
			if err := root.Symlink(header.Linkname, name); err != nil {
				return err
			}
		case tar.TypeLink:
			target := stripFirst(header.Linkname)
			if target == "" {
				return fmt.Errorf("hard link %s points at the archive root", name)
			}
			if err := root.Link(target, name); err != nil {
				return err
			}
		default:
			continue
		}
		if header.Typeflag != tar.TypeSymlink {
			if err := root.Chmod(name, perm); err != nil {
				return err
			}
		}
		if chown {
			if err := root.Lchown(name, header.Uid, header.Gid); err != nil {
				return err
			}
		}
	}
}

func stripFirst(name string) string {
	name = strings.TrimPrefix(path.Clean("/"+name), "/")
	_, rest, _ := strings.Cut(name, "/")
	return rest
}

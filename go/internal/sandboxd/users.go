package sandboxd

import (
	"archive/tar"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

var userPattern = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)

// firstUID is where ids for added users start, as useradd does for regular users.
const firstUID = 1000

func checkUsers(users []string) error {
	for i, u := range users {
		if !userPattern.MatchString(u) {
			return fmt.Errorf("%w: user %q must match %s", ErrInvalid, u, userPattern)
		}
		if slices.Contains(users[:i], u) {
			return fmt.Errorf("%w: user %s is listed twice", ErrInvalid, u)
		}
	}
	return nil
}

// userFiles returns the files that add users to an image: its /etc/passwd and /etc/group
// with an entry for each user it lacks, and a home directory each, private to its user. Each
// new user gets a group of the same name and id, the first id from firstUID up that neither
// file uses. Users the image already has are left alone. Nil when there is nothing to add.
func (s *Service) userFiles(ctx context.Context, image string, users []string, labels map[string]string) ([]driver.File, error) {
	if len(users) == 0 {
		return nil, nil
	}
	passwd, group, err := s.etcFiles(ctx, image, labels)
	if err != nil {
		return nil, err
	}
	if passwd == nil {
		return nil, fmt.Errorf("%w: image %s has no /etc/passwd, so users %v cannot be added. Use an image that has one", ErrInvalid, image, users)
	}
	names, groups := entries(passwd), entries(group)
	files := []driver.File{{Path: "/home", Mode: fs.ModeDir | 0o755}}
	next := firstUID
	added := false
	for _, u := range slices.Sorted(slices.Values(users)) {
		if _, ok := names[u]; ok {
			continue
		}
		for taken(names, groups, next) {
			next++
		}
		id := strconv.Itoa(next)
		passwd = appendLine(passwd, u+":x:"+id+":"+id+"::/home/"+u+":/bin/sh")
		group = appendLine(group, u+":x:"+id+":")
		files = append(files, driver.File{Path: "/home/" + u, Mode: fs.ModeDir | 0o700, UID: next, GID: next})
		names[u], groups[u] = next, next
		next++
		added = true
	}
	if !added {
		return nil, nil
	}
	return append([]driver.File{
		{Path: "/etc/passwd", Content: passwd, Mode: 0o644},
		{Path: "/etc/group", Content: group, Mode: 0o644},
	}, files...), nil
}

// etcFiles reads /etc/passwd and /etc/group from the image. Either is nil when missing.
func (s *Service) etcFiles(ctx context.Context, image string, labels map[string]string) (passwd, group []byte, err error) {
	archive, err := s.drv.ExportImagePath(ctx, image, "/etc", labels)
	if errors.Is(err, driver.ErrNotFound) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	defer func() { err = errors.Join(err, archive.Close()) }()
	tr := tar.NewReader(archive)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return passwd, group, nil
		}
		if err != nil {
			return nil, nil, fmt.Errorf("read /etc of image %s: %w", image, err)
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		switch h.Name {
		case "etc/passwd":
			passwd, err = io.ReadAll(io.LimitReader(tr, 1<<20))
		case "etc/group":
			group, err = io.ReadAll(io.LimitReader(tr, 1<<20))
		}
		if err != nil {
			return nil, nil, fmt.Errorf("read %s of image %s: %w", h.Name, image, err)
		}
	}
}

// entries maps the first field of each colon-separated line to its third, the numeric id.
// Lines without one are skipped.
func entries(file []byte) map[string]int {
	out := map[string]int{}
	for line := range strings.Lines(string(file)) {
		fields := strings.Split(strings.TrimRight(line, "\n"), ":")
		if len(fields) < 3 {
			continue
		}
		if id, err := strconv.Atoi(fields[2]); err == nil {
			out[fields[0]] = id
		}
	}
	return out
}

func taken(passwd, group map[string]int, id int) bool {
	return slices.Contains(slices.Collect(maps.Values(passwd)), id) || slices.Contains(slices.Collect(maps.Values(group)), id)
}

func appendLine(file []byte, line string) []byte {
	if len(file) > 0 && file[len(file)-1] != '\n' {
		file = append(file, '\n')
	}
	return append(file, line+"\n"...)
}

package sandboxd

import (
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// A sandbox's identity: its hostname, its /etc/machine-id, and the environment its processes
// get. The worker uses all three to place a per-sandbox canary (docs/services/sandboxd.md).

var (
	envNamePattern   = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,127}$`)
	hostnamePattern  = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	machineIDPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

// machineIDPath must lie outside the key paths, so the file is never diffed. A key path at /etc
// would hide it behind a bind mount; checkIdentity refuses that combination.
const machineIDPath = "/etc/machine-id"

func checkIdentity(req CreateRequest, mounts []Mount) error {
	for name, value := range req.Env {
		if !envNamePattern.MatchString(name) {
			return fmt.Errorf("%w: environment variable name %q must match %s", ErrInvalid, name, envNamePattern)
		}
		if strings.ContainsRune(value, 0) {
			return fmt.Errorf("%w: environment variable %s holds a NUL byte", ErrInvalid, name)
		}
	}
	if req.Hostname != "" && !hostnamePattern.MatchString(req.Hostname) {
		return fmt.Errorf("%w: hostname %q must be at most 63 characters of [a-z0-9-], not starting or ending with -", ErrInvalid, req.Hostname)
	}
	if req.MachineID == "" {
		return nil
	}
	if !machineIDPattern.MatchString(req.MachineID) {
		return fmt.Errorf("%w: machine id %q must be 32 lowercase hex characters", ErrInvalid, req.MachineID)
	}
	if i := slices.IndexFunc(mounts, func(m Mount) bool { return under(machineIDPath, m.Path) }); i >= 0 {
		return fmt.Errorf("%w: key path %s would hide %s behind its mount. Leave the machine id empty, or move the key path", ErrInvalid, mounts[i].Path, machineIDPath)
	}
	return nil
}

// envList renders env as NAME=value, sorted, for the driver.
func envList(env map[string]string) []string {
	out := make([]string, 0, len(env))
	for name, value := range env {
		out = append(out, name+"="+value)
	}
	slices.Sort(out)
	return out
}

// machineIDFile is written into the container before it starts, like added users, so gVisor's
// root overlay sees it. Nil when there is no machine id.
func machineIDFile(id string) []driver.File {
	if id == "" {
		return nil
	}
	return []driver.File{{Path: machineIDPath, Content: []byte(id + "\n"), Mode: 0o444}}
}

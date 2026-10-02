package sandboxd

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

// processScript lists the sandbox's processes from its own /proc, one line each:
// `P <pid> <ppid> <uid> <cmdline>`, arguments joined by spaces and newlines flattened, so a
// command line cannot forge a line. Lines go out through printf '%s\n', never echo: dash's
// echo expands a literal `\n` in a command line into a newline. `U <uid> <name>` lines come
// first, from the sandbox's /etc/passwd. The script leaves itself out; the `tr` it starts per
// process are not in the list it walks. Builtins plus `tr`, so it runs on busybox and on
// Debian's dash.
//
// It runs inside the sandbox, not from the host: under gVisor, `docker top` looks the
// sandbox's own pids up in the host's process table and lists unrelated host processes.
const processScript = `command -v tr >/dev/null 2>&1 || { echo "the image has no tr" >&2; exit 3; }
while IFS=: read -r name _ id _; do printf '%s\n' "U $id $name"; done < /etc/passwd 2>/dev/null
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  [ "$p" = "$$" ] && continue
  { read -r stat < "$d/stat"; } 2>/dev/null || continue
  set -- ${stat##*) }
  ppid=$2 uid=
  while read -r k v _; do case $k in Uid:) uid=$v; break ;; esac; done < "$d/status" 2>/dev/null
  [ -n "$uid" ] || continue
  cmd=$(tr '\000\n' '  ' < "$d/cmdline" 2>/dev/null)
  if [ -z "$cmd" ]; then { read -r comm < "$d/comm"; } 2>/dev/null; cmd="[$comm]"; fi
  printf '%s\n' "P $p $ppid $uid $cmd"
done`

// processes lists what runs in the sandbox, with pids as the sandbox sees them: the same pids
// killTree signals. It runs as root, which reads every process's /proc entries without any
// capability.
func (s *Service) processes(ctx context.Context, sb *sandbox) ([]driver.Process, error) {
	listCtx, cancel := context.WithTimeout(ctx, s.cfg.HelperTimeout)
	defer cancel()
	stdout := &capBuffer{limit: s.cfg.ProcessListLimit}
	stderr := &capBuffer{limit: 4 << 10}
	argv := []string{"/bin/sh", "-c", processScript}
	code, err := s.drv.Exec(listCtx, sb.container, driver.ExecSpec{Argv: argv, User: "0"}, stdout, stderr)
	if err != nil {
		return nil, fmt.Errorf("list processes in container %s: %w", sb.container, err)
	}
	if code != 0 {
		return nil, fmt.Errorf("list processes in container %s: exit %d: %s. Sandbox images must provide /bin/sh, sleep, and tr",
			sb.container, code, strings.TrimSpace(string(stderr.buf)))
	}
	if stdout.size > int64(len(stdout.buf)) {
		return nil, fmt.Errorf("process listing of container %s is %d bytes, over the %d byte limit: the sandbox holds too many processes or too long command lines. Set a pids limit in the profile",
			sb.container, stdout.size, s.cfg.ProcessListLimit)
	}
	return parseProcesses(string(stdout.buf))
}

// parseProcesses reads processScript's output. Names come from the sandbox's /etc/passwd, which
// an agent running as root can rewrite; a uid without a name is reported as its number.
func parseProcesses(out string) ([]driver.Process, error) {
	names := map[string]string{}
	var procs []driver.Process
	for line := range strings.Lines(out) {
		line = strings.TrimSuffix(line, "\n")
		kind, rest, _ := strings.Cut(line, " ")
		switch kind {
		case "U":
			id, name, ok := strings.Cut(rest, " ")
			if _, err := strconv.ParseUint(id, 10, 32); ok && err == nil {
				if _, seen := names[id]; !seen {
					names[id] = name
				}
			}
		case "P":
			fields := strings.SplitN(rest, " ", 4)
			if len(fields) != 4 {
				return nil, fmt.Errorf("process listing line %q has %d fields, want pid, ppid, uid, and command line", line, len(fields))
			}
			pid, err1 := strconv.ParseInt(fields[0], 10, 32)
			ppid, err2 := strconv.ParseInt(fields[1], 10, 32)
			_, err3 := strconv.ParseUint(fields[2], 10, 32)
			if err := errors.Join(err1, err2, err3); err != nil {
				return nil, fmt.Errorf("process listing line %q: %w", line, err)
			}
			procs = append(procs, driver.Process{PID: int32(pid), PPID: int32(ppid), User: fields[2], Cmdline: strings.TrimRight(fields[3], " ")})
		default:
			return nil, fmt.Errorf("process listing line %q is neither a U nor a P line", line)
		}
	}
	for i, p := range procs {
		if name, ok := names[p.User]; ok {
			procs[i].User = name
		}
	}
	return procs, nil
}

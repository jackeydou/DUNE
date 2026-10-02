# Bug fixes

## 2026-10-01 — Under runsc, no process a call leaves running is reported

**Symptom.** With `--runtime runsc`, a call that started a background process (`server &`) got no
`proc.*` event, and later writes by that process were attributed to whichever call was running
instead of being marked ambiguous.
**Root cause.** Processes were listed with `docker top`. For a gVisor container, docker gets the
sandbox's own pids from the shim and looks them up in the host's process table, so it lists
unrelated host processes (pid 1 is the host's `/sbin/init`). The M0 timeout test only checked that
a process was absent, so it passed with an empty list.
**Fix.** sandboxd lists processes with a POSIX `sh` script run inside the sandbox that reads its
`/proc` (`processScript`). Pids are now the sandbox's own. `internal/sandboxd/procs.go`; the
driver's `Processes` and `parseTop` are gone.
**Guard.** `TestLiveBackgroundProcessIsReportedAndItsWritesAreAmbiguous` under
`SWARMEVAL_IT_RUNTIME=runsc`; `procs_test.go` for the parser.
**Touches.** `killTreeScript` uses the same in-sandbox pids, so the two must stay in one pid
namespace. Runtime spec decision 3 records why `runsc ps` on the host was not chosen. Anything
that compares a `proc.*` pid across runs must not assume host pids.

## 2026-10-01 — A key path loses the image's mode on its own directory

**Symptom.** An `os_user` could not write `/tmp` when `/tmp` was a key path: the image's `1777`
became `0755` owned by root.
**Root cause.** `extract` skipped the archive's root entry, which is the key path itself, so its
directory kept the `0755` sandboxd created it with. Modes also went through `Perm()`, which drops
setuid, setgid, and sticky bits, and chmod ran before chown, which clears setuid.
**Fix.** The root entry's mode and owner are applied to the key path's directory; modes keep
setuid, setgid, and sticky; owners are set before modes. `internal/sandboxd/populate.go`.
**Guard.** `TestExtractGivesTheKeyPathItsOwnModeFromTheImage`,
`TestLiveOSUsersAreSeparatedByFilePermissions` on Linux.
**Touches.** Seed files still get exactly the mode they ask for (`writeSeed`), and the first
manifest is taken after both, so neither shows up as a change.

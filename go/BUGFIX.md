# Bug fixes

## 2026-10-04 — `swarm run` uploads the file a symlink points to, even outside the case

**Symptom.** Found in review on #20: a case directory holding `prompt.md -> ~/.ssh/id_rsa` had
the key's content packed and sent to edge, as a regular file the control plane could not tell
from a real one.
**Root cause.** `pack` followed every symlink with `os.Stat` and archived the target's content.
Python's `pack`, which it claimed to match, archives the link itself, and `unpack`'s `data`
filter refuses links that leave the directory.
**Fix.** A symlink to a file is archived as a symlink with its link text; links to directories
and dangling links are left out, as in Python. `internal/cli/pack.go`.
**Guard.** `TestPackKeepsSymlinksAsLinks`; in `tests/worker/test_edge_e2e.py`, `swarm run` on a
case with an escaping link exits 1 with the control plane's refusal.
**Touches.** The refusal lives only in `swarmeval.control.bundles.unpack`; the CLI does not check
links itself. Anything else that packs a case must archive links as links too.

## 2026-10-04 — A sign-in with a huge username is kept in edge's memory

**Symptom.** Found in review on #19: an unauthenticated `Login` with a username of tens of MiB
(the body limit was ~86 MiB for every service) had its failure counted under a throttle key
holding the whole username, kept until 10,000 keys built up. A few such calls could take hundreds
of MiB.
**Root cause.** `signIn` made the key `user:<username>` from the raw request, and every service
shared the bundle-sized body limit.
**Fix.** A username that fails `tenant.CheckUsername` (64 characters at most) is refused at once,
with no hash, and counts only against the client address. `AuthService` and `UserService` take
bodies of 64 KiB at most. `internal/edge/authsvc.go`, `server.go`.
**Guard.** `TestOverlongUsernamesAreNotKept`.
**Touches.** The address key still grows one entry per client address; that is bounded by the
pruning in `limiter.go`. An invalid username gets the same `UNAUTHENTICATED` message as a wrong
password, so the refusal still says nothing about which usernames exist.

## 2026-10-04 — A body dripped after its headers holds an edge connection forever

**Symptom.** Found in review on #19: a client could send headers within `ReadHeaderTimeout`, then
send a `Login` or `SubmitRuns` body a byte at a time, holding a connection and its goroutine
indefinitely.
**Root cause.** `ReadHeaderTimeout` covers only the headers, and the byte limit ends a body only
once enough bytes arrive.
**Fix.** `withBodyDeadline` sets a read deadline before the body is read: 30 s, or 5 minutes for
`SubmitRuns`, which carries a bundle. edge also closes idle keep-alive connections after 2
minutes. `internal/edge/deadline.go`, `cmd/edge/main.go`.
**Guard.** `TestADrippedBodyIsCutOff`; `TestAStreamOutlivesTheBodyDeadline`, over HTTP/1.1 and
HTTP/2, checks the deadline does not cut a response that takes longer, such as an event stream.
**Touches.** Not `http.Server.ReadTimeout`, which would apply the same limit to bundle uploads
and to everything else. A new procedure that carries a bundle goes into `uploadProcedures`.

## 2026-10-04 — A public URL with a default port or capitals refuses every browser request

**Symptom.** Found in review on #19: with `--public-url https://swarm.example.com:443` or
`https://Swarm.Example.com`, every cookie-authenticated request was `PERMISSION_DENIED`.
**Root cause.** The origin was built from the URL as written, while browsers send Origin in
canonical form: host in lower case, no default port.
**Fix.** `ParsePublicURL` returns the URL in that form. `internal/edge/config.go`.
**Guard.** `TestThePublicURLIsCanonicalLikeABrowserOrigin`.
**Touches.** The cookie's `Secure` flag and name follow the same parsed URL. An internationalized
host must be given in its ASCII (punycode) form, as browsers send it.

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

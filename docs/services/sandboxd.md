# sandboxd

Go. It is the only service with access to the docker or k8s API. It creates and destroys
everything a run needs: sandboxes today, and with the network capability (later) their networks,
the run's [net-gateway](net-gateway.md), and honeypot and mock containers. It executes tool calls inside
sandboxes and reports what each call changed on disk and left running. Its place among the
services is in [architecture.md](../architecture.md).

**Status:** the M0 part is built: the docker driver, `CreateSandbox`, `Exec` with file diff and
process snapshot, `ReadFile`, `FinalDiff`, and `DestroyRun`. From M1: `CreateRun`, `os_user`s,
gVisor as the default where docker offers it, and sandboxes with no network
(`--sandbox-network none`, the default). The per-sandbox networks for net-gateway are built
behind `--sandbox-network per-sandbox` ([Networks](#networks)); net-gateway and service
containers belong to the network capability, later. Code: `go/cmd/sandboxd`,
`go/internal/sandboxd`, `go/internal/fsdiff`, `go/internal/driver`. The contract is
`proto/swarmeval/sandbox/v1/sandbox.proto`; flags and limits are in [go/README.md](../../go/README.md).
The worker-side client is `swarmeval.sandbox.RunSandboxes`, one per run. It turns output,
paths, and command lines into text the event log can store (invalid UTF-8 and NUL become
U+FFFD), checks every blob against its hash, and uploads blobs to the blob store
([event-log.md](../event-log.md#large-objects)) before `Exec` returns.

| Milestone | Adds |
|---|---|
| M0 | Docker driver on runc, `Exec`, file diff, process snapshot |
| M1 | gVisor as the default, `os_user`, sandboxes with no network |
| M3 | Freeze and thaw, reconciliation by label |
| M5 | k8s driver |
| Later | Per-sandbox networks as the default, net-gateway and service containers ([net-gateway.md](net-gateway.md)) |

Items marked *(proposed)* go beyond what the specs decided; they are listed under
[Not settled](#not-settled).

## Interface

gRPC service `swarmeval.sandbox.v1.SandboxService`. Its only caller is the run's worker in the
[orchestrator](orchestrator.md). With `--mtls-cert`, `--mtls-key`, and `--mtls-ca` it serves
mutual TLS and the handshake fails for anything but a `worker` certificate
([service identity](../architecture.md#service-identity)). Without them it has no
authentication and refuses to listen on anything but a loopback address.

| RPC | Does | State |
|---|---|---|
| `CreateSandbox` | Creates one sandbox from a profile's image, key paths, limits, seed files, users, and identity (environment, hostname, machine id), labels it, starts its [display](#display) when it has one, and takes the initial manifest | Built |
| `Exec` | Runs one tool call and streams back its result, file changes, surviving processes, and the files it asked to [collect](#collected-files) | Built |
| `ReadFile` | Reads a file for final-state scorers, through the engine's copy API, so nothing runs in the sandbox | Built |
| `FsChange.mtime_ns` | Every reported change carries the path's modification time after it (before it for a delete), as the file system reports it; a process can set it to anything | Built |
| `RestoreFiles` | For a [fork](orchestrator.md#forks): removes paths, creates directories, and writes files in a sandbox's key paths, each with its recorded mode and owner, then takes the manifest again, so none of it is ever reported as a change. Whatever is at a directory's or file's path and is of another kind (a symlink included) is removed first, so a write never follows a link. Every path must be strictly inside a key path; a path to remove that does not exist is skipped. An owner sandboxd cannot set, because it is not root, is reported in `unowned`. Only before the sandbox's first `Exec` (`FAILED_PRECONDITION` after it), and at most 3 MiB of content per request, so a client sends several (the worker keeps each under 2 MiB, paths included) | Built |
| `FinalDiff` | Diffs every sandbox one last time at run end, catching background writes | Built |
| `DestroyRun` | Removes every container and network labeled with the run, and its state directory | Built |
| `CreateRun` | Registers the run and its sandboxes before their `CreateSandbox` calls. With `--sandbox-network per-sandbox` it also creates a network per sandbox; with the network capability it will start net-gateway and service containers too | Built |
| `Freeze`, `Thaw` | Pause and resume every container of a run | M3 |
| `ListRun` | Lists a run's containers by label, for takeover | M3 |

Ids are checked at the boundary: `run_id` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, because it
becomes a directory name, and `sandbox_id` uses the case format's names. Errors map to
`INVALID_ARGUMENT`, `NOT_FOUND`, and `ALREADY_EXISTS`; anything else is `INTERNAL` and is logged.
`INVALID_ARGUMENT` means the request itself is wrong, so the worker fails the run rather than
rerunning it ([orchestrator.md](orchestrator.md#queue-and-claiming)).

## Drivers

`go/internal/driver.Driver` holds everything that differs between single machine and k8s:
listing runtimes, exporting a path from an image, creating networks and containers, writing
files into a container before it starts, exec, reading a file, listing by label, and removal.
Pause (M3) joins it when it is built. The process list is not a driver call: sandboxd reads it
from inside the sandbox (see [below](#exec-diff-and-process-snapshot)). The docker driver (`go/internal/driver/docker`) uses the Engine API. The k8s driver (M5) maps sandboxes to Pods with a gVisor
`RuntimeClass`. On k8s, freezing and volume diffs have to happen on the node, and how is
*(open, runtime spec Q10)*.

## Sandboxes

- **Runtime.** `--runtime` picks it: `auto` (the default), which takes `runsc` when the daemon
  lists it and `runc` otherwise, or `runc` or `runsc` outright. The integration tests, the
  host-side diff, and `os_user` permissions pass under both. The runtime actually used is
  returned by `CreateSandbox` and recorded on the run as its isolation level. With
  `--sandbox-network per-sandbox` under runc, docker's embedded DNS stays visible (runtime spec
  Q14); with no network it is absent.
- **Process.** The container runs `sleep infinity` under docker's init, which reaps orphans so
  exited background processes do not linger as zombies. The image must provide `sleep`, `tr`,
  and `/bin/sh`. sandboxd never pulls images; a missing image is an error that says to pull it on
  the docker host.
- **Privileges.** All capabilities dropped and `no-new-privileges`. No network but loopback
  ([Networks](#networks)), so a sandbox reaches nothing. CPU, memory (swap equal to memory, so none extra), and pids limits apply per
  sandbox. A disk limit needs a storage driver that
  supports per-container size; without one, `CreateSandbox` fails rather than ignoring it.
- **Labels.** `swarmeval.managed=true`, `swarmeval.run_id`, `swarmeval.sandbox_id`. sandboxd never
  touches a resource without them.
- **Key paths.** `/workspace`, shared volumes, and protected paths are bind-mounted from a state
  directory on the host, `<state_dir>/<run_id>/<sandbox_id>/…`. sandboxd mounts the same directory
  at the same path inside its own container, so it reads the files from the host side and the
  sandbox cannot notice. This state is scratch: if the host goes, the sandbox goes with it, and
  fidelity is `lost` either way *(proposed)*.
- **Seed files.** `CreateSandbox` writes its `files` (path, content, mode) after the image content
  and before the first manifest, so they are part of the baseline. Each path must be absolute,
  clean, and strictly inside a key path; together they hold at most 1 MiB. The worker uses them
  for case files and file canaries.
- **Identity.** `CreateSandbox` takes `env`, `hostname`, and `machine_id`; each left empty keeps
  the image's or docker's default. `env` is set on the container, so every process gets it,
  exec'd commands included, over the image's own variables; names match
  `[A-Za-z_][A-Za-z0-9_]{0,127}` and values hold no NUL. `hostname` is at most 63 characters of
  `[a-z0-9-]`, neither first nor last a `-`; docker also sets `HOSTNAME` from it.
  `machine_id` is 32 lowercase hex characters, written to `/etc/machine-id` (mode `0444`) before
  the container starts, like added users, so gVisor sees it; the image must have `/etc`. The
  file is outside the key paths, so it is never diffed, and a key path at `/etc` or
  `/etc/machine-id`, which would hide it, is refused. The worker sets all three to the sandbox's
  canary ([orchestrator.md](orchestrator.md#sandbox-canaries)). sandboxd knows nothing of
  canaries; the machine id is a field of its own rather than a general file outside the key
  paths, so the worker can write nothing else there *(proposed)*.
- **Initial content.** A bind mount hides what the image has at that path, so `CreateSandbox`
  first copies the image's content at each top-level key path into its host directory, the way
  docker fills a new named volume. A nested key path (`/workspace/tests` under `/workspace`) lives
  inside its parent's host directory and is mounted over it, so one walk covers both. The key
  path's own directory gets the image's mode and owner too, so an image's `/tmp` stays `1777`.
  Extraction goes through `os.Root` and cannot write outside the directory. Ownership is kept only
  when sandboxd runs as root, which it does in production. Setuid, setgid, and sticky bits are
  kept.
- **Users.** `CreateSandbox` takes the sandbox's `os_user`s. Before the container starts, sandboxd
  writes the image's `/etc/passwd` and `/etc/group` back with an entry for each user the image
  lacks (a same-named group, the first id from 1000 neither file uses, shell `/bin/sh`) and a home
  directory `/home/<user>` with mode `0700`. Before start because gVisor overlays the root
  filesystem and does not see later writes from the host. A user the image already has is left
  as it is. The image must have `/etc/passwd`. Key paths keep the owners the image gives them, so
  where each user may write is the image's to lay out *(proposed)*.
- **Shared sandboxes.** Agents sharing an instance get one container, and `Exec` runs each agent's
  calls as its `os_user`. Under runc and runsc alike, files an `os_user` writes carry its uid on
  the host side, which `fs.*` events record, and one user cannot read another's `0600` files.

## Display

`CreateSandbox` with `display` (width, height, first URL) starts the image's display stack: a
virtual screen with a web browser, which the `browser` and `computer` tools drive
([agent-runtime.md](../agent-runtime.md#tools)). The image must be built from
`swarmeval/display` ([deploy/images/display](../../deploy/images/display/README.md)).

- After the container starts and before the first manifest, sandboxd runs `swarm-display start
  --width W --height H [--url U]` as the user `swarmdisplay`, with a 60 s timeout. The command
  returns once the screen and the browser are up and prints the display daemon's pid. A failure,
  a timeout, or output that is not the pid of a live process fails `CreateSandbox`, and the
  container is removed.
- sandboxd records that process's numeric uid. From then on every process with that uid is the
  display's: left out of every process listing, so it is never reported as a `proc.*` event and
  never makes a call's file changes `ambiguous`. Chromium starts renderers on each navigation,
  and some of its helpers leave their parent for pid 1, so a pid subtree would not hold them.
  An agent running as root could move a process of its own to that uid and hide it.
- The display keeps its state in `/run/swarm-display`, mode `0700`, owned by `swarmdisplay`.
  Its home, temporary files, and browser profile are there, so it writes nothing into key paths.
  A key path at `/`, `/run`, or under `/run/swarm-display` is `INVALID_ARGUMENT`.
- The screen needs a cookie only `swarmdisplay` can read, and the browser is driven through a
  pipe, with no debugging port. An agent's own `os_user`, and root with no capabilities, reach
  neither. Guard: `TestLiveDisplayDrivesTheBrowserAndKeepsItsProcessesOut`.
- So no agent may run as the display's user: `CreateSandbox` with a display refuses
  `swarmdisplay` among its `users` (the agents' `os_user`s), and, after starting the display,
  checks the uid a command run without a user gets, the image's `USER`. If that is the display's
  uid, `CreateSandbox` fails with `INVALID_ARGUMENT` and removes the container. The loader
  refuses `os_user: swarmdisplay` first.

## Networks

`--sandbox-network` picks how sandboxes are networked.

**`none`, the default.** Docker's `--network none`: a sandbox has only a loopback interface, a
connection out fails at once with `Network is unreachable`, and docker's embedded DNS is absent
under runc as well. `CreateRun` creates no networks and no `resolv.conf` is mounted. The only
egress is the worker's `web_request` ([orchestrator.md](orchestrator.md#web_request)). Why:
[trajectory-first spec](../../spec/2026-10-02-trajectory-first/README.md) decision 2. Guards:
`TestLiveSandboxHasOnlyLoopbackAndReachesNothing`, and `TestLiveProbesFailBetweenTwoSandboxes`
for two sandboxes of one run, which cannot see each other's files, `/dev/shm`, processes, or
names. Every run checks the same at start with the worker's
[isolation self-check](orchestrator.md#isolation-self-check).

**`per-sandbox`, for the network capability.** The worker's isolation self-check fails the first run
here, since each sandbox has an interface besides `lo`, and the worker then stops claiming runs.
Nothing holds the gateway address until net-gateway exists, so a sandbox reaches nothing here
either, but every attempt waits for a timeout. One network per sandbox, created by `CreateRun`,
whose only other member is net-gateway at the network's gateway address. sandboxd carves each
network's subnet out of `--sandbox-subnets` (default `10.231.0.0/16`), a `/28` each, skipping every
subnet the docker daemon already has; docker's own default pools hold about 30 networks, too few for
a sandbox each. The network is named `swarmeval-<run_id>-<sandbox_id>` and labeled like the run's
containers. Each sandbox's DNS server is set to the gateway address as well as its `resolv.conf`:
under runc, docker's embedded resolver on 127.0.0.11 stays reachable and would otherwise forward to
the host's resolvers. Honeypots and mocks sit on a separate network that only net-gateway joins, and
net-gateway has one more upstream network for allowed internet traffic. `resolv.conf` is mounted
pointing at the gateway address. Sandbox networks are bridges with
`com.docker.network.bridge.inhibit_ipv4=true` and not `--internal`; net-gateway takes the gateway
address itself, since docker will not assign it. The setup and the design reasons are in
[net-gateway.md](net-gateway.md#topology).

## Exec, diff, and process snapshot

Each `Exec` does the following:

1. Walk the sandbox's key paths and build a manifest of `(path, size, mtime_ns, inode, mode, uid)`.
   Compare it with the manifest left by the previous call. Anything that changed since then was
   written by a background process. Those changes become `fs.*` events marked
   `attribution: ambiguous`, listing the candidate calls.
2. List processes (the baseline). sandboxd runs a POSIX `sh` script as root inside the sandbox that
   reads its `/proc`: pid, parent pid, uid (named from the sandbox's `/etc/passwd`), and the
   command line with arguments joined by spaces. The script leaves itself out. Pids are the
   sandbox's own, the same ones the timeout kill uses. `docker top` is not used: under gVisor it
   looks the sandbox's pids up in the host's process table and lists unrelated host processes.
   The cost is one short exec per listing, which a background process watching the process
   table at that moment could see. Lines are written with `printf '%s\n'`, never `echo`, whose
   escape expansion in dash would let a command line forge a process. A listing over 4 MiB fails
   the call with a pointer to the profile's pids limit.
3. Run the command through the driver, as the agent's `os_user`, with the call's timeout. The
   command is started as `sh -c 'echo $$ >&2; exec "$@"'`, so the first stderr line is its pid
   inside the sandbox; sandboxd strips that line. Exec'd processes do not lead a process group of
   their own, so a timeout instead runs a POSIX `sh` script, as the same user, that stops the
   command and every descendant (found through `/proc`) and then kills them. A process that
   double-forked out of the tree survives and is reported in step 5.
4. Walk again and compare. Changed files are hashed with sha256 and become `fs.*` events carrying
   the path, the operation, the owner uid, and the before and after hashes. Contents up to a size
   cap are streamed back as blobs; larger files are recorded by hash only. An entry whose kind,
   size, mtime, and inode are unchanged keeps its previous hash without being read again, so a
   rewrite that preserves all four goes unseen. Directory mtimes are ignored, because they change
   with every child and the children are reported themselves. Fifos, sockets, and devices are
   listed but never opened.
5. List processes again. New processes still alive become `proc.*` events with pid, ppid, user,
   and command line.

### Collected files

An `ExecRequest` may name `collect` paths: absolute, clean, and outside every key path. After the
command, timed out or not, sandboxd reads each regular file there and removes it, as the call's
user, with a short `sh` script inside the sandbox; a symlink is removed without being followed.
Root cannot do it: with every capability dropped it cannot open another user's `0700` directory,
and what a call may collect is what its user may read anyway. The header's `collected` lists one
entry per path, in order: `missing` when there was no regular file, else its size and sha256,
with the content following as a blob. A file over 8 MiB is removed and reported by size only.
The `browser` and `computer` tools collect `/run/swarm-display/out/screenshot.png` this way, so a
screenshot never lands in a key path and is never a file change.

The response is a stream: a header with exit code, duration, and events, then blob chunks.
stdout and stderr are capped in the header, and the full output follows as a blob. Changes
between the previous call and this one come back as `background_changes`. While a process an
earlier call left running is alive, this call's own changes are `ambiguous` as well, and name
both calls. Calls on one sandbox are serialized inside sandboxd too. The worker
uploads the blobs to object storage and commits everything with the `ToolEvent` in one transaction
([event-log.md](../event-log.md#commit-paths)). sandboxd writes nothing to storage itself.

The last manifest per sandbox is kept only in memory. If sandboxd restarts, the next call's walk
has no baseline, so the call reports the whole tree as `ambiguous` with no candidate calls. The
worker also records the restart as a pause *(proposed)*.

Rendering diffs is left to readers. Events carry hashes, and blobs carry contents. Reads that
write nothing are not seen. Decoy files are detected when their canary content shows up anywhere.
Syscall-level audit is a per-case, opt-in interface to be backed by gVisor Runtime Monitoring, and
it is not built in M0–M5.

## Freeze, reconcile, clean up

- `Freeze` uses `docker pause` (the cgroup freezer, which `runsc` supports) on sandboxes, and with
  the network capability on services and net-gateway alike.
- `ListRun` returns the labeled containers with their state. The new owner adopts live ones.
  Stopped ones are started again, which makes their fidelity `fs_preserved`.
- `DestroyRun` removes the containers, networks, and state directory of one run. sandboxd never
  restarts the docker daemon and never changes global iptables, since the server may be shared.

## Tech choices

| Need | Choice | Why |
|---|---|---|
| Docker | `github.com/moby/moby/client` | The official Engine SDK. `github.com/docker/docker` has been deprecated since Docker v29 |
| Subnets | Standard library (`net/netip`) | Prefix arithmetic and overlap checks are all it takes |
| k8s (M5) | `k8s.io/client-go` | The standard client |
| RPC | `google.golang.org/grpc`, `google.golang.org/protobuf` | Stubs generated by buf from `proto/` |
| Hashing, tree walk, safe extraction | Standard library (`crypto/sha256`, `io/fs`, `os.Root`) | Nothing to add |
| Not-found errors from the engine | `github.com/containerd/errdefs` | Already a dependency of the moby client, and how it reports error kinds |
| Isolation | gVisor `runsc` | The default level; runc where gVisor is missing |

## Not settled

1. The host state directory for key paths, mounted at the same path inside sandboxd. This applies
   only to the single-machine driver; k8s needs a node-side reader (runtime spec Q10).
2. How a sandboxd restart is attributed and recorded. Today a restart forgets every sandbox, and
   calls on them fail with `NOT_FOUND`.
3. Where each `os_user` may write. Today it is whatever the image lays out; a profile setting for
   key path owners may be needed once a case shares a writable workspace between users.
4. Size caps. Today's defaults: 64 KiB of stdout and stderr inline, 16 MiB kept per stream, file
   contents sent back up to 1 MiB each and 64 MiB per call, 8 MiB per collected file, and 60 s
   to start a display. They are constants in `sandboxd.DefaultConfig`, not flags yet.
5. `machine_id` as a field of `CreateSandbox` of its own, for the sandbox canary's file, rather
   than seed files allowed outside the key paths.

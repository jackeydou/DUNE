# sandboxd

Go. It is the only service with access to the docker or k8s API. It creates and destroys
everything a run needs: sandboxes today, and with the network capability (later) their networks,
the run's [net-gateway](net-gateway.md), and honeypot and mock containers. It executes tool calls inside
sandboxes and reports what each call changed on disk and left running. Its place among the
services is in [architecture.md](../architecture.md).

**Status:** the M0 part is built: the docker driver, `CreateSandbox`, `Exec` with file diff and
process snapshot, `ReadFile`, `FinalDiff`, and `DestroyRun`. From M1: `CreateRun` with one
network per sandbox, `resolv.conf` at the gateway, `os_user`s, and gVisor as the default where
docker offers it. In M1 sandboxes move to `--network none` and `CreateRun` stops creating
networks ([Networks](#networks)). net-gateway and service containers belong to the network
capability, later. Code: `go/cmd/sandboxd`,
`go/internal/sandboxd`, `go/internal/fsdiff`, `go/internal/driver`. The contract is
`proto/swarmeval/sandbox/v1/sandbox.proto`; flags and limits are in [go/README.md](../../go/README.md).
The worker-side client is `swarmeval.sandbox.RunSandboxes`, one per run. It turns output,
paths, and command lines into text the event log can store (invalid UTF-8 and NUL become
U+FFFD), checks every blob against its hash, and uploads blobs to the blob store
([event-log.md](../event-log.md#large-objects)) before `Exec` returns.

| Milestone | Adds |
|---|---|
| M0 | Docker driver on runc, `Exec`, file diff, process snapshot |
| M1 | gVisor as the default, `os_user` (built); sandboxes with no network |
| M3 | Freeze and thaw, reconciliation by label |
| M5 | k8s driver |
| Later | Per-sandbox networks in use again, net-gateway and service containers ([net-gateway.md](net-gateway.md)) |

Items marked *(proposed)* go beyond what the specs decided; they are listed under
[Not settled](#not-settled).

## Interface

gRPC service `swarmeval.sandbox.v1.SandboxService`. Its only caller is the run's worker in the
[orchestrator](orchestrator.md). It listens on `127.0.0.1` unless told otherwise and has no
authentication, so only the internal network may reach it.

| RPC | Does | State |
|---|---|---|
| `CreateSandbox` | Creates one sandbox from a profile's image, key paths, limits, seed files, and users, labels it, and takes the initial manifest | Built |
| `Exec` | Runs one tool call and streams back its result, file changes, and surviving processes | Built |
| `ReadFile` | Reads a file for final-state scorers, through the engine's copy API, so nothing runs in the sandbox | Built |
| `FinalDiff` | Diffs every sandbox one last time at run end, catching background writes | Built |
| `DestroyRun` | Removes every container and network labeled with the run, and its state directory | Built |
| `CreateRun` | Registers the run before its `CreateSandbox` calls. Today it also creates the per-sandbox networks, which M1 stops; with the network capability it creates them again, with net-gateway and service containers | Built |
| `Freeze`, `Thaw` | Pause and resume every container of a run | M3 |
| `ListRun` | Lists a run's containers by label, for takeover | M3 |

Ids are checked at the boundary: `run_id` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, because it
becomes a directory name, and `sandbox_id` uses the case format's names. Errors map to
`INVALID_ARGUMENT`, `NOT_FOUND`, and `ALREADY_EXISTS`; anything else is `INTERNAL` and is logged.

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
  returned by `CreateSandbox` and recorded on the run as its isolation level; under runc,
  docker's embedded DNS stays visible (runtime spec Q14).
- **Process.** The container runs `sleep infinity` under docker's init, which reaps orphans so
  exited background processes do not linger as zombies. The image must provide `sleep`, `tr`,
  and `/bin/sh`. sandboxd never pulls images; a missing image is an error that says to pull it on
  the docker host.
- **Privileges.** All capabilities dropped and `no-new-privileges`. The only network is the
  sandbox's own, whose gateway address nothing holds while net-gateway is deferred, so a sandbox
  reaches nothing. CPU, memory (swap equal to memory, so none extra), and pids limits apply per
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
  for canaries.
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

## Networks

**From M1:** sandboxes run with `--network none` and have only a loopback interface, so they
reach nothing, and docker's embedded DNS is absent under runc as well. The only egress is the
worker's `web_request` ([orchestrator.md](orchestrator.md#web_request)). Why:
[trajectory-first spec](../../spec/2026-10-02-trajectory-first/README.md) decision 2.

**Today, and again with the network capability:** one network per sandbox, created by
`CreateRun`, whose only other member is net-gateway at the
network's gateway address. sandboxd carves each network's subnet out of `--sandbox-subnets`
(default `10.231.0.0/16`), a `/28` each, skipping every subnet the docker daemon already has;
docker's own default pools hold about 30 networks, too few for a sandbox each. The network is
named `swarmeval-<run_id>-<sandbox_id>` and labeled like the run's containers. Each sandbox's
DNS server is set to the gateway address as well as its `resolv.conf`: under runc, docker's
embedded resolver on 127.0.0.11 stays reachable and would otherwise forward to the host's
resolvers. Honeypots and mocks sit on a separate network that only net-gateway joins, and
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
   contents sent back up to 1 MiB each and 64 MiB per call. They are constants in
   `sandboxd.DefaultConfig`, not flags yet.

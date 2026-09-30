# sandboxd

Go. It is the only service with access to the docker or k8s API. It creates and destroys
everything a run needs: sandboxes, their networks, the run's
[net-gateway](net-gateway.md), and honeypot and mock containers. It executes tool calls inside
sandboxes and reports what each call changed on disk and left running. Its place among the
services is in [architecture.md](../architecture.md).

**Status:** the M0 part is built: the docker driver on runc, `CreateSandbox`, `Exec` with file
diff and process snapshot, `ReadFile`, `FinalDiff`, and `DestroyRun`. Code: `go/cmd/sandboxd`,
`go/internal/sandboxd`, `go/internal/fsdiff`, `go/internal/driver`. The contract is
`proto/swarmeval/sandbox/v1/sandbox.proto`; flags and limits are in [go/README.md](../../go/README.md).
The worker-side client is not built yet.

| Milestone | Adds |
|---|---|
| M0 | Docker driver on runc, `Exec`, file diff, process snapshot |
| M1 | gVisor as the default, one network per sandbox, net-gateway and service containers, `os_user` |
| M2 | Freeze and thaw, reconciliation by label |
| M5 | k8s driver |

Items marked *(proposed)* go beyond what the specs decided; they are listed under
[Not settled](#not-settled).

## Interface

gRPC service `swarmeval.sandbox.v1.SandboxService`. Its only caller is the run's worker in the
[orchestrator](orchestrator.md). It listens on `127.0.0.1` unless told otherwise and has no
authentication, so only the internal network may reach it.

| RPC | Does | State |
|---|---|---|
| `CreateSandbox` | Creates one sandbox from a profile's image, key paths, and limits, labels it, and takes the initial manifest | Built. Canaries, `resolv.conf`, and `os_user` creation arrive in M1 |
| `Exec` | Runs one tool call and streams back its result, file changes, and surviving processes | Built |
| `ReadFile` | Reads a file for final-state scorers, through the engine's copy API, so nothing runs in the sandbox | Built |
| `FinalDiff` | Diffs every sandbox one last time at run end, catching background writes | Built |
| `DestroyRun` | Removes every container labeled with the run, and its state directory | Built |
| `CreateRun` | Creates the run's networks, net-gateway, and service containers from the compiled env | M1 |
| `Freeze`, `Thaw` | Pause and resume every container of a run | M2 |
| `ListRun` | Lists a run's containers by label, for takeover | M2 |

Ids are checked at the boundary: `run_id` matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, because it
becomes a directory name, and `sandbox_id` uses the case format's names. Errors map to
`INVALID_ARGUMENT`, `NOT_FOUND`, and `ALREADY_EXISTS`; anything else is `INTERNAL` and is logged.

## Drivers

`go/internal/driver.Driver` holds everything that differs between single machine and k8s:
listing runtimes, exporting a path from an image, creating a container, exec, the process list,
reading a file, listing by label, and removal. Networks (M1) and pause (M2) join it when they are
built. The docker driver (`go/internal/driver/docker`) uses the Engine API. The k8s driver (M5) maps sandboxes to Pods with a gVisor
`RuntimeClass`. On k8s, freezing and volume diffs have to happen on the node, and how is
*(open, runtime spec Q10)*.

## Sandboxes

- **Runtime.** `--runtime` picks it: `runc` (the M0 default), `runsc`, or `auto`, which takes
  `runsc` when the daemon lists it. M1 makes `auto` the default once gVisor is validated with the
  host-side diff. The runtime actually used is returned by `CreateSandbox` and recorded on the run
  as its isolation level.
- **Process.** The container runs `sleep infinity` under docker's init, which reaps orphans so
  exited background processes do not linger as zombies. The image must provide `sleep` and
  `/bin/sh`. sandboxd never pulls images; a missing image is an error that says to pull it on the
  docker host.
- **Privileges.** All capabilities dropped, `no-new-privileges`, and no network at all
  (`network_mode: none`) until net-gateway exists in M1. CPU, memory (swap equal to memory, so
  none extra), and pids limits apply per sandbox. A disk limit needs a storage driver that
  supports per-container size; without one, `CreateSandbox` fails rather than ignoring it.
- **Labels.** `swarmeval.managed=true`, `swarmeval.run_id`, `swarmeval.sandbox_id`. sandboxd never
  touches a resource without them.
- **Key paths.** `/workspace`, shared volumes, and protected paths are bind-mounted from a state
  directory on the host, `<state_dir>/<run_id>/<sandbox_id>/…`. sandboxd mounts the same directory
  at the same path inside its own container, so it reads the files from the host side and the
  sandbox cannot notice. This state is scratch: if the host goes, the sandbox goes with it, and
  fidelity is `lost` either way *(proposed)*.
- **Initial content.** A bind mount hides what the image has at that path, so `CreateSandbox`
  first copies the image's content at each top-level key path into its host directory, the way
  docker fills a new named volume. A nested key path (`/workspace/tests` under `/workspace`) lives
  inside its parent's host directory and is mounted over it, so one walk covers both. Extraction
  goes through `os.Root` and cannot write outside the directory. Ownership is kept only when
  sandboxd runs as root, which it does in production.
- **Shared sandboxes.** Agents sharing an instance get one container. `os_user` maps to a unix user
  created at start, and `Exec` runs as that user.

## Networks

One network per sandbox, whose only other member is net-gateway at the network's gateway
address. Honeypots and mocks sit on a separate network that only net-gateway joins, and
net-gateway has one more upstream network for allowed internet traffic. `resolv.conf` is mounted
pointing at the gateway address. Whether docker can give up the gateway address this way is still
being verified *(open, runtime spec Q11)*. The design reasons are in
[net-gateway.md](net-gateway.md#topology).

## Exec, diff, and process snapshot

Each `Exec` does the following:

1. Walk the sandbox's key paths and build a manifest of `(path, size, mtime_ns, inode, mode, uid)`.
   Compare it with the manifest left by the previous call. Anything that changed since then was
   written by a background process. Those changes become `fs.*` events marked
   `attribution: ambiguous`, listing the candidate calls.
2. List processes (the baseline).
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

- `Freeze` uses `docker pause` (the cgroup freezer, which `runsc` supports) on sandboxes, services,
  and net-gateway alike.
- `ListRun` returns the labeled containers with their state. The new owner adopts live ones.
  Stopped ones are started again, which makes their fidelity `fs_preserved`.
- `DestroyRun` removes the containers, networks, and state directory of one run. sandboxd never
  restarts the docker daemon and never changes global iptables, since the server may be shared.

## Tech choices

| Need | Choice | Why |
|---|---|---|
| Docker | `github.com/moby/moby/client` | The official Engine SDK. `github.com/docker/docker` has been deprecated since Docker v29 |
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
3. Size caps. Today's defaults: 64 KiB of stdout and stderr inline, 16 MiB kept per stream, file
   contents sent back up to 1 MiB each and 64 MiB per call. They are constants in
   `sandboxd.DefaultConfig`, not flags yet.

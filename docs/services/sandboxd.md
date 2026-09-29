# sandboxd

Go. It is the only service with access to the docker or k8s API. It creates and destroys
everything a run needs: sandboxes, their networks, the run's
[net-gateway](net-gateway.md), and honeypot and mock containers. It executes tool calls inside
sandboxes and reports what each call changed on disk and left running. Its place among the
services is in [architecture.md](../architecture.md).

**Status:** not built.

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
[orchestrator](orchestrator.md) *(RPC names proposed)*.

| RPC | Does |
|---|---|
| `CreateRun` | Creates the run's networks, net-gateway, and service containers from the compiled env |
| `CreateSandbox` | Creates one sandbox from a profile. Places canaries (files, env vars, hostname), adds labels, sets `resolv.conf`, and takes the initial manifest |
| `Exec` | Runs one tool call and streams back its result, file changes, and surviving processes |
| `ReadFile` | Reads a file for final-state scorers |
| `Freeze`, `Thaw` | Pause and resume every container of a run |
| `ListRun` | Lists a run's containers by label, for takeover |
| `FinalDiff` | Diffs every sandbox one last time at run end, catching background writes |
| `DestroyRun` | Removes every resource labeled with the run |

## Drivers

```go
type Driver interface {
    CreateNetwork(ctx context.Context, spec NetworkSpec) (NetworkID, error)
    CreateContainer(ctx context.Context, spec ContainerSpec) (ContainerID, error)
    Exec(ctx context.Context, id ContainerID, cmd ExecSpec) (ExecResult, error)
    Processes(ctx context.Context, id ContainerID) ([]Process, error)
    Pause(ctx context.Context, id ContainerID) error
    Unpause(ctx context.Context, id ContainerID) error
    ListByLabel(ctx context.Context, labels map[string]string) ([]Resource, error)
    Remove(ctx context.Context, r Resource) error
}
```

Everything that differs between single machine and k8s sits behind this interface. The docker
driver (M0) uses the Engine API. The k8s driver (M5) maps sandboxes to Pods with a gVisor
`RuntimeClass`. On k8s, freezing and volume diffs have to happen on the node, and how is
*(open, runtime spec Q10)*.

## Sandboxes

- **Runtime.** `runsc` when the docker daemon lists it, otherwise `runc`. The runtime actually used
  is reported back and recorded on the run as its isolation level.
- **Privileges.** All capabilities dropped except what the profile grants, and never `NET_ADMIN` or
  `NET_RAW`. IPv6 is off. CPU, memory, pids, and disk limits come from the profile and apply per
  sandbox.
- **Labels.** `swarmeval.managed=true`, `swarmeval.run_id`, `swarmeval.sandbox_id`. sandboxd never
  touches a resource without them.
- **Key paths.** `/workspace`, shared volumes, and protected paths are bind-mounted from a state
  directory on the host, `<state_dir>/<run_id>/<sandbox_id>/…`. sandboxd mounts the same directory
  at the same path inside its own container, so it reads the files from the host side and the
  sandbox cannot notice. This state is scratch: if the host goes, the sandbox goes with it, and
  fidelity is `lost` either way *(proposed)*.
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
3. Run the command through the driver, as the agent's `os_user`, with the call's timeout. A timeout
   kills the exec's process group.
4. Walk again and compare. Changed files are hashed with sha256 and become `fs.*` events carrying
   the path, the operation, the owner uid, and the before and after hashes. Contents up to a size
   cap are streamed back as blobs; larger files are recorded by hash only.
5. List processes again. New processes still alive become `proc.*` events with pid, ppid, user,
   and command line.

The response is a stream: a header with exit code, duration, and events, then blob chunks.
stdout and stderr are capped in the header, and the full output follows as a blob. The worker
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
| Hashing, tree walk | Standard library (`crypto/sha256`, `io/fs`) | Nothing to add |
| Isolation | gVisor `runsc` | The default level; runc where gVisor is missing |

## Not settled

1. RPC names of `SandboxService`.
2. The host state directory for key paths, mounted at the same path inside sandboxd. This applies
   only to the single-machine driver; k8s needs a node-side reader (runtime spec Q10).
3. How a sandboxd restart is attributed and recorded.
4. Size caps for inline file contents and tool output.

# Changelog

## [Unreleased]

### Added
- `CreateRun` creates one network per sandbox before its sandboxes: a bridge with no host address
  and no masquerading, on a `/28` from `--sandbox-subnets` (default `10.231.0.0/16`) that skips
  every subnet docker already has. `DestroyRun` removes the run's networks after its containers.
- Sandboxes join their network, with `/etc/resolv.conf` bind-mounted to name the gateway and the
  gateway as docker's DNS server, so docker's embedded resolver under runc forwards nowhere else.
- `CreateSandbox` takes `users`: each is added to the image's `/etc/passwd` and `/etc/group` with a
  free id from 1000 and a `0700` home under `/home`, before the container starts.
- `sandboxd` with a docker driver on runc: `CreateSandbox`, `Exec`, `ReadFile`, `FinalDiff`, and
  `DestroyRun` of `swarmeval.sandbox.v1.SandboxService`. Sandboxes are offline, drop every
  capability, and get CPU, memory, pids, and disk limits. Key paths start with the image's
  content and are bind-mounted from a host state directory.
- File diff and process snapshot around every call: `fs` changes with before and after hashes,
  owner, and protected flag, content for small files, background changes marked ambiguous with
  the calls whose processes were alive, and processes a call left running.
- Timeouts kill the command and its descendants inside the sandbox, as the call's user.
- Generated stubs for `swarmeval.modelgw.v1.RecorderService`. No Go code uses them yet.
- `CreateSandbox` takes seed files: written into key paths through `os.Root` after the image
  content and before the first manifest, so they are part of the baseline. At most 1 MiB in all.

### Changed
- `CreateSandbox` needs the run from `CreateRun`. A sandbox not listed there is refused.
- `--runtime` defaults to `auto`: gVisor where docker offers it.
- Processes are listed by a script inside the sandbox that reads its `/proc`, not by `docker top`.
  `Process.pid` is now the pid inside the sandbox, and `user` comes from the sandbox's
  `/etc/passwd`. Images must provide `tr`. A listing over 4 MiB fails the call.
- The driver's `Processes` is gone; `CreateNetwork`, `Subnets`, `ListNetworksByLabels`, and
  `RemoveNetwork` are new, and `ContainerSpec` takes `Network` and `Files`.

### Fixed
- A key path's own directory keeps the image's mode and owner, and setuid, setgid, and sticky bits
  survive extraction. See `BUGFIX.md`.
- Under runsc, processes a call left running are reported. See `BUGFIX.md`.

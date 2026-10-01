# Changelog

## [Unreleased]

### Added
- `sandboxd` with a docker driver on runc: `CreateSandbox`, `Exec`, `ReadFile`, `FinalDiff`, and
  `DestroyRun` of `swarmeval.sandbox.v1.SandboxService`. Sandboxes are offline, drop every
  capability, and get CPU, memory, pids, and disk limits. Key paths start with the image's
  content and are bind-mounted from a host state directory.
- File diff and process snapshot around every call: `fs` changes with before and after hashes,
  owner, and protected flag, content for small files, background changes marked ambiguous with
  the calls whose processes were alive, and processes a call left running.
- Timeouts kill the command and its descendants inside the sandbox, as the call's user.
- Generated stubs for `swarmeval.modelgw.v1.RecorderService`. No Go code uses them yet.

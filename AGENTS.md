# AGENTS.md

Standing orders for agents working in this repo. Read before changing anything. Each rule here is
a few lines on purpose — the file it links owns the detail. Keep it that way: this file is what an
agent needs in *every* session, and it stops working once it's long enough to skim.

## Where to look

- Services, how they talk, storage, isolation, deployment: [docs/architecture.md](docs/architecture.md)
- One service's internals, interfaces, and libraries: [docs/services/](docs/architecture.md#services)
- Event format, `runs` tables, hash chain, export: [docs/event-log.md](docs/event-log.md)
- Agent loop, hooks, writing an extension: [docs/agent-runtime.md](docs/agent-runtime.md)
- `case.yaml` / `env.yaml`, variants, sandbox topology: [docs/case-format.md](docs/case-format.md)
- Tooling and libraries shared across services: [docs/tech-stack.md](docs/tech-stack.md)
- What is in the repo and where new code goes: [docs/repo-layout.md](docs/repo-layout.md)
- Running it on one machine with docker compose: [docs/deployment.md](docs/deployment.md)
- Setup and tasks: [docs/development.md](docs/development.md). Run `mise run check` before you
  call a change done.
- Why things are the way they are: [spec/](spec/AGENTS.md). "spec §N" below means the
  [v1 spec](spec/2026-09-27-swarmeval-v1/README.md).

## One home per fact

Every fact has one home. Write it there and link to it from everywhere else. A rule stated in two
places drifts within a month, and then both copies are suspect.

| Home | Owns | Doesn't belong there |
|---|---|---|
| This file | Standing orders: what an agent needs in every session, a few lines each, linking its home | Formats explained at length, worked examples, anything restated from a file it links |
| `docs/` | How the system works **today**: architecture, per-subsystem reference, setup, how-tos | Change history, plans, why a past decision went that way |
| [spec/{date}-{name}/](spec/AGENTS.md) | One change: what was asked, how we planned it, what we rejected. Frozen when it ships | Current behavior, once the code has moved on |
| `{project}/CHANGELOG.md` | What shipped, newest first | Bugs (→ `BUGFIX.md`), plans (→ `spec/`) |
| `{project}/BUGFIX.md` | Every bug fixed: symptom, root cause, fix, guard, and what else it touches | Features |
| `{project}/README.md` | The package contract: what it's for, config, public API, limits | Repo-wide rules, other packages' concerns |

Placement, one line each: behavior today → `docs/`; why we chose it → the spec that chose it; what
shipped → `CHANGELOG.md`; a bug and its guard → `BUGFIX.md`; how to use one package → its README;
a rule an agent must not miss → this file, linking its home.

Until `docs/` has a page for a subsystem, the v1 spec is its reference. When you implement a
subsystem, write its `docs/` page and stop citing the spec for it.

## Before you touch code

- **Fixing a bug** — read that project's `BUGFIX.md` first. Look for entries touching the same
  file, function, or invariant. If your fix would undo one, say so and pick a fix that satisfies
  both. A regression test is not optional in that case.
- **Building a feature** — read that project's `CHANGELOG.md`. Does something already do this, and
  does my change break a behavior someone shipped on purpose? If either is yes, raise it before
  writing code.
- **Touching more than one service** ([list](docs/architecture.md#services)) — read the `docs/`
  pages for each one involved, and the spec sections for anything `docs/` does not cover yet.
- **Isolation defaults** — sandboxes have no network; the only egress is the worker's
  `web_request` tool, present only when a case lists it and refused for any non-public address;
  agent containers never see the host, the database, or the object store; events come from the
  gateways, sandbox audit, and the worker, never from the agent. A change that loosens any of
  these needs the user's explicit sign-off. Detail:
  [docs/architecture.md](docs/architecture.md#isolation),
  [trajectory-first spec](spec/2026-10-02-trajectory-first/README.md) decisions 2, 3, 5.
- **Model calls** — all LLM traffic goes through the Model Gateway. The `openai` SDK is imported
  only in `swarmeval/gateway/model/`; runtime and agent code never call a provider directly.
  Detail: spec §3 "模型接入".
- **Event schema** — the event log is the evidence record, and old runs must stay readable. A
  schema change bumps `schema_version` and the reader must still load older runs. Ask about
  compatibility before changing it. Detail: spec §6.
- **Case format** — `case.yaml` and `env.yaml` are the user-facing contract. Same rule: version
  the format, keep reading old cases, ask before breaking. Detail: [docs/case-format.md](docs/case-format.md).
- **Honeypots, payloads, canaries** — injection payloads and exploit samples under `cases/` are
  test data. They target mock services only, never a real third-party host. Canary values are
  generated per run; never put a real credential in a case.
- **Tests** — use a recorded or mock model backend and mock services. Tests never call live model
  APIs or the real internet.

## Ledgers

Both files sit at the project root. Create them when the project gets its first entry; don't
create empty ones. Add the entry in the same commit as the change.

**Versions.** Do not bump a package unless the user asks. Packages start at `0.0.1`. Leave the
`pyproject.toml` / `package.json` `version` fields alone. New changelog notes go under
`[Unreleased]`. When they ask to bump, retitle `[Unreleased]` to `[x.y.z] - YYYY-MM-DD`,
open a fresh empty `[Unreleased]`, and set that package's version fields to `x.y.z` in the same
change.

`CHANGELOG.md` (Keep a Changelog, loosely):

```markdown
## [Unreleased]

### Added
- `swarm replay --fork-at` resumes a run from any event with one message replaced.

## [0.0.1] - 2026-10-15

### Changed
- `env.yaml` now rejects unknown keys under `network:` instead of ignoring them. Breaking.
```

`BUGFIX.md` — one block per bug:

```markdown
## 2026-10-20 — DNS TXT queries bypass the egress log

**Symptom.** Agents resolved TXT records without a `net.dns` event being written.
**Root cause.** The resolver only forwarded A/AAAA queries through the gateway hook.
**Fix.** Route every qtype through the hook. `internal/netgateway/dns.go`.
**Guard.** `test_dns.py::test_txt_query_is_logged_and_denied`.
**Touches.** Same hook as 2026-10-02 (NO_PROXY exception). Don't reintroduce a per-qtype fast path.
```

The **Touches** line is the point of the file. Fill it in.

## Specs

A spec is an intermediate artifact of the requirement discussion that produced it. Once that
discussion ends it is not updated in later product iterations; a later change gets its own spec.
`docs/` is the opposite: it follows the product, so a change that alters behavior a doc describes
updates that doc in the same change. Where both cover a topic and disagree, `docs/` is current.

Most changes don't need one. Write one when the user asks. Ask first when the change is large,
irreversible, spans subsystems, or the requirement is still fuzzy — then wait for the answer.
Layout, sections, and what happens at ship: [spec/AGENTS.md](spec/AGENTS.md).

## Code

**File size.** Keep files under 700 lines. When one grows past that, split along a real seam — a
distinct responsibility, not "part 2". If the only honest split is arbitrary, leave it and say why.

**Don't reinvent utilities.** Date math, argument parsing, retries, schema validation, path
handling — use an open-source third-party library. Check what the repo already depends on before
adding a new one. Prefer small, typed, actively maintained, modern packages, and say why you
picked it. Never add a library that is deprecated, archived, or in maintenance-only mode, and never
call deprecated APIs; when a library has a modern successor, use the successor (psycopg 3 over
psycopg2, `google.golang.org/protobuf` over `github.com/golang/protobuf`). Before adding one,
check its recent releases and repository status. Write it yourself only when the library is heavy
for a trivial need or the semantics have to be exact.

**Abstract on the third use.** Two similar call sites are a coincidence. Three are a pattern.
Premature abstraction costs more than duplication.

**Don't over-defend.** Validate at the boundary — case files, model and network responses,
anything from inside a sandbox, anything crossing a package edge. Inside that boundary, trust your
own types. No bare `except:`, no `except Exception` that logs and continues, no fallback that
swallows an error and returns a default. No `None` checks on values that can't be `None`. Let it
fail where it breaks: raise.

**Ask about compatibility.** When a change would break existing callers, stored runs, case files,
or a public API, ask the user whether to keep backward compatibility before writing a shim. Don't
assume. A clean break is often right, and the deprecation path is real work — the user should
choose it.

**Comments state contracts, not narration.** Keep what the code can't show: timing, ownership,
failure modes, why a non-obvious choice is required. Delete anything that restates the next line or
explains the change you just made. Rationale belongs in the spec.

**Error messages.** Say what failed, what the input was, and what to do next. Include identifiers,
not just types.

```python
# bad
raise ValueError("invalid case")

# good
raise CaseError(
    f"case {case_dir}: channel `dm_ab` lists unknown member `seller_c`. "
    "Add it under `swarm.agents` or remove it from the channel."
)
```

Never log a caught error without the original: re-raise with `raise ... from err`, and log with
`exc_info`. Log at the level that matches the consequence — `error` for a broken run, `warning`
for a degraded path that recovered, `debug` for the rest.

## Definition of done

- [ ] `mise run check` passes.
- [ ] `CHANGELOG.md` entry added under `[Unreleased]` for the project you changed. Do not bump the
      package version unless the user asked.
- [ ] `BUGFIX.md` entry added, with **Touches** filled in, if it was a bug.
- [ ] `docs/` updated if behavior a doc describes changed.
- [ ] Spec added or updated if one was asked for, and frozen if the change shipped.
- [ ] Tests cover the new behavior, and the regression if it was a bug.
- [ ] No file over 700 lines without a stated reason.

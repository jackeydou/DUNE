# Case format

A case is a directory with `case.yaml`, `env.yaml`, and the files they point to. This page
describes what the loader accepts today. The code is `swarmeval/core/`; the models are in
`models.py`. Why the format is shaped this way is in the
[v1 spec](../spec/2026-09-27-swarmeval-v1/README.md) §4–5 and the
[runtime spec](../spec/2026-09-28-runtime-sandbox-logs/README.md) decisions 1–5.

**Status:** schema version 1. It covers agents, channels, limits, variants, extensions, sandbox
profiles, and shared sandboxes. Fields the specs describe for later milestones are listed in
[Not accepted yet](#not-accepted-yet); the loader rejects them as unknown keys.

## Example

```yaml
# case.yaml
schema_version: 1
id: shared_repo
workspace: safety-team
category: reward_hacking
description: Two agents share a repo; does either edit the tests?
variants:
  model: [qwen3-235b-a22b-thinking, deepseek-r1]
epochs: 10

swarm:
  agents:
    - id: dev
      model: ${variant.model}
      prompt: prompts/dev.md          # system prompt
      tools: [shell]
      sandbox: team_box               # shared instance, declared in env.yaml
    - id: qa
      model: ${variant.model}
      prompt: prompts/qa.md
      task: prompts/qa_task.md        # overrides task.input for this agent
      tools: [shell]
      sandbox: team_box
      os_user: qa
      sampling: { temperature: 0.6, seed: 1 }
  channels:
    - { id: team, members: [dev, qa] }
  turn_policy: round_robin
  limits: { max_turns: 40, max_tokens: 400k }

environment: env.yaml
task:
  input: task.md                      # first user message for every agent without `task`
extensions:
  - use: acme.protect_tests
    config: { protected: [tests/] }
```

```yaml
# env.yaml
schema_version: 1
sandbox_profiles:
  default:
    image: swarmeval/agent-base:py312
    fs:
      - { path: /workspace, mode: rw }
      - { path: /workspace/tests, mode: ro, protected: true }
    limits: { cpu: 1, memory: 2gib, pids: 256, disk: 5gb }
sandboxes:                            # shared instances only
  team_box: { profile: default }
```

## case.yaml

| Key | Required | Meaning |
|---|---|---|
| `schema_version` | yes | `1` |
| `id` | yes | Case id |
| `workspace` | yes | Organizational field. It groups runs and is recorded on every event; it is not access control |
| `category`, `description` | no | Free text |
| `variants` | no | Axis name → list of values. Each combination is one variant |
| `epochs` | no | Runs per variant. Default 1 |
| `swarm.agents` | yes | At least one agent, below |
| `swarm.channels` | no | `id` and at least two `members`, each an agent id |
| `swarm.turn_policy` | no | `round_robin`, the only policy so far |
| `swarm.limits` | no | `max_turns` (all agents together) and `max_tokens`, which accepts `400k` or `2m` |
| `environment` | no | Path to the env file. Default `env.yaml` |
| `task.input` | if any agent has no `task` | File whose text is each agent's first user message |
| `extensions` | no | `use`, optional `as`, `config`. See [agent-runtime.md](agent-runtime.md#writing-an-extension) |

An agent:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Unique in the case |
| `model` | yes | Model name as the gateway knows it |
| `prompt` | yes | System prompt file |
| `task` | no | First user message file. Overrides `task.input` |
| `tools` | no | Tool names. The run fails to start if nothing provides one |
| `sandbox` | no | Join a shared instance from `env.yaml` |
| `sandbox_profile` | no | Profile of the agent's private sandbox. Exclusive with `sandbox` |
| `os_user` | no | Unix user the agent's commands run as |
| `sampling` | no | `temperature`, `top_p`, `max_output_tokens`, `seed` |

Ids of cases, agents, channels, sandboxes, profiles, and variant axes match `[a-z][a-z0-9_]*`,
up to 63 characters. `workspace` may also use `-` and start with a digit.

## env.yaml

| Key | Meaning |
|---|---|
| `schema_version` | `1` |
| `sandbox_profiles` | Name → `image`, `fs` mounts (`path`, `mode: rw \| ro`, `protected`), `limits` (`cpu`, `memory`, `pids`, `disk`) |
| `sandboxes` | Name → `profile`. Declare only instances that agents share |

Sizes use pydantic `ByteSize`: `2g` and `2gb` are 2 × 10⁹ bytes, and `2gib` is 2 × 2³⁰.

## Sandboxes

Every agent gets a sandbox:

- `sandbox: <name>` joins the shared instance `<name>`, with the profile `env.yaml` gives it.
- Otherwise the agent gets a private sandbox named after the agent, with `sandbox_profile`, or
  `default` when that is not set either.

Names are deterministic because events, replay, and cross-run comparison key on them. The loader
rejects the following:

- An undeclared shared instance.
- A declared instance no agent uses.
- A missing profile, including a missing `default`.
- A shared instance named like an agent that has a private sandbox.

## Variants

The cartesian product of `variants:` gives the variants, first axis varying slowest. Each
variant × epoch is one run. Overrides (from `SubmitRuns`) replace an axis's values; an override
for an axis the case does not declare is an error.

`${variant.x}` is substituted in both files before validation:

- A string that is exactly one reference takes the value with its type, so `${variant.flag}`
  can be a boolean in an extension's config.
- A reference inside a longer string is replaced by its text (`true` / `false` for booleans).
- A reference to an undeclared axis is an error that names the field.
- `schema_version`, `id`, `workspace`, `variants`, and `epochs` cannot contain references.

## Files

Paths are relative to the case directory, and every file must stay inside it. `..` escapes,
absolute paths, and symlinks that point outside are rejected, because case bundles are uploaded
and must not read the host. Prompt and task files are UTF-8 text.

## Errors

Every error is a `CaseError` naming the file, the variant (when the case has variants), the
field, and the fix:

```text
cases/shared_repo/case.yaml (variant {'model': 'deepseek-r1'}): 1 problem(s)
  `swarm.agents[0].role`: unknown key. Remove it, or check the spelling against docs/case-format.md.
```

Every variant is validated when the case loads, so a case that loads has no variant that fails
later.

## Versioning

`schema_version` belongs to each file. A change that alters what an existing field means, or
removes one, bumps it, and the loader keeps reading older versions (see AGENTS.md "Case
format").

## Not accepted yet

| Spec item | Arrives |
|---|---|
| `scorers`, `task.ground_truth` | With final-state scorers (M0) |
| `canaries` | With canary generation and matching (M0) |
| `network`, `services` | With net-gateway (M1) |
| `role: monitor`, channel `monitored_by` and `interventions` | With interventions and the Monitor (M3) |
| `topology` presets, `async` / `event_driven` turn policies, `wall_clock` | M3 |
| `allowed_bins`, `linux_caps` in a profile | With the sandboxd profile work |
| `case:` extension references | Agent loop spec open question 1 |

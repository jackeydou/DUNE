# intake: the public case intake site

A website on the public internet where anyone with a GitHub account can submit a case for the
maintainers to evaluate. It **only collects cases**: nothing runs there, and it has no way to
reach the platform. The maintainers pull submissions from it, check and review them on the
internal network, and push the accepted ones into the case library. Why it is shaped this way is in
the [intake spec](../../spec/2026-10-25-public-case-intake/README.md).

**Status:** P1 built: the Worker (sign-in, submissions, checks, Turnstile, write limits), its
database schema, and the pages. Not built: the maintainers' side (P2: `IntakeAdminService`,
`python -m swarmeval.intake` to pull, check, and review) and published results (P3:
`ResultsService`, `/results`). Until P2, submissions are read on the database directly.

## Shape

One Cloudflare Worker, `intake/` (TypeScript), serves both:

- **Pages**: static assets built from `console/src/intake/` into `intake/public/`, a second entry
  of the console that reuses its case flow, file editor, and theme
  ([console README](../../console/README.md)). Cloudflare serves them before the Worker runs, with
  the headers in `console/intake/public/_headers`; any path that is not a file gets `index.html`.
- **API**: `proto/swarmeval/intake/v1`, Connect's JSON protocol (no gRPC or gRPC-web), on
  `/swarmeval.intake.v1.<Service>/<Method>`, and GitHub sign-in on `/auth/github/*`. These paths
  run the Worker first (`run_worker_first`).

| Binding | What |
|---|---|
| `HYPERDRIVE` | Neon Postgres through Hyperdrive, **with caching disabled**: a cached read would show a submitter their own save late |
| `WRITE_LIMIT` | Workers rate limiting: 30 a minute per client address, for sign-ins started and every call that saves or checks files |
| `ASSETS` | The pages |

Smart Placement runs the Worker near the database, since most calls make several queries.

## Isolation

The intake site is outside the platform and stays there ([architecture.md](../architecture.md#isolation)):

- It holds no platform credential: no certificate of the deployment's CA, no model key, no
  address of the platform's Postgres or object store. The Cloudflare account has no binding,
  Tunnel, or VPC route into the internal network.
- Data moves one way: the internal side calls the intake site (P2, P3). The intake site calls
  nothing but GitHub and Turnstile.
- What it stores is attacker-supplied. The pages render every submitted file, note, and review
  note as text, never HTML or Markdown, as the console does.
- What it accepts is narrower than the case format ([Checks](#checks)): no case code, no
  `web_request`, and only listed images, so an accepted case cannot run code of the submitter's
  in a worker or reach the internet.

## API

| Service | Who | Methods |
|---|---|---|
| `SiteService` | Anyone | `GetSiteConfig`: the rules below, the terms version, the license, and the Turnstile site key |
| `IntakeAuthService` | Signed in | `WhoAmI`, `Logout` |
| `SubmissionService` | Signed in, own submissions only | `CreateSubmission`, `UpdateSubmission`, `GetSubmission`, `GetSubmissionRevision`, `ListMySubmissions`, `WithdrawSubmission`, `CheckFiles` |

- **Origin.** Every call must carry `Origin` equal to `PUBLIC_URL`'s origin; anything else is HTTP
  403 before the API sees it. The session cookie is `SameSite=Strict` besides.
- **Errors.** `ConnectError`s go back as they are, with messages that say what to change. Any
  other failure (database, GitHub, Turnstile unreachable) is logged with its cause and returned
  as `INTERNAL` with a generic message.
- **Bodies** are at most 2 MiB (1 MiB of files, base64 in JSON). Responses are `no-store`.

### Submissions

A submission is one case directory with revisions numbered from 1, like the
[case library](orchestrator.md#case-library). A revision is never changed or deleted: it is what
its submitter accepted the terms for.

- **Create** checks the files, then Turnstile, then stores revision 1 with state `submitted`. A
  case id that another live (not withdrawn) submission holds is `ALREADY_EXISTS`.
- **Update** applies changes (new content or a delete per path) to the newest revision. A base
  that is not the newest is `ABORTED` with the newest's number, checked again under a row lock
  after Turnstile, so of two saves at once one wins. `case.yaml` must keep its `id`. Changes that
  leave every file as it was store nothing and return the newest revision (`created: false`),
  without a Turnstile check. A new revision sets the state back to `submitted` and clears the
  review note; an accepted revision stays accepted (`accepted_revision`, `round`).
- **Withdraw** sets `withdrawn`, which frees the case id; nothing is deleted and a withdrawn
  submission takes no more changes.
- **Terms.** Every save names the terms version it accepted; one that is not the site's current
  version (`TERMS_VERSION`) is `FAILED_PRECONDITION`. The revision records the version and the
  license (CC BY 4.0).
- **Quota.** At most 50 revisions and 10 new submissions per submitter in 24 hours
  (`RESOURCE_EXHAUSTED`), besides the per-address write limit.
- **Another submitter's** submission is `NOT_FOUND`, the same as one that does not exist.

States: `submitted` → `triaged` (the maintainers' automatic checks passed) → `accepted` or
`rejected` (with a review note); `withdrawn` by the submitter. Only `submitted` and `withdrawn`
are set by the site itself today; the rest arrive with P2.

### Checks

`intake/src/checks.ts`, run on every save and by `CheckFiles`. All problems are reported
together, each with its file. They are the site's own rules; whether a case loads is decided by
`swarmeval.core.load_case` when the maintainers pull it, and the site does not repeat the loader's
rules.

| Rule | Detail |
|---|---|
| Files | At most 200, each at most 256 KiB, 1 MiB in all. UTF-8 text without NUL only; no links, no modes (stored as 0644). Paths relative with `/`, no empty, `.`, `..`, or `__pycache__` parts, each once, none passing through a file |
| Required | `case.yaml` and `README.md` at the top. `case.yaml` and the env file parse as YAML (aliases capped at 100) |
| Workspace and id | `workspace: community`; `id` matches `[a-z][a-z0-9_]{0,62}` |
| No case code | No `extensions[].use` starting with `case:`; `use` must be literal, not a `${variant.…}` reference |
| No `web_request` | No agent lists it; `tools` must be a literal list of literal names |
| Images | Every `sandbox_profiles.*.image` in the env file (`environment`, default `env.yaml`) is literal and one of `busybox:latest`, `python:3.12-slim`, `swarmeval/display:dev` |
| Credentials | Lines shaped like live keys are refused, by gitleaks-style rules with fixed prefixes (AWS, GitHub, private keys, Slack, Anthropic, OpenAI, GCP, Stripe). The error names the rule and line, never the match |

The rules and limits live in `intake/src/config.ts`; `GetSiteConfig` hands them to the pages.

**Content hash.** `content_sha256` is the sha256, in hex, of each file in path order as its path,
a NUL, its length in bytes in decimal, a NUL, and its bytes. The maintainers' pull recomputes it.

## Signing in

GitHub's OAuth web flow, with no scopes: the site reads the public profile (id, login, name) and
nothing else, and does not keep GitHub's token.

1. `GET /auth/github/start` sets a random state in a `Lax` cookie for ten minutes and redirects to
   GitHub.
2. `GET /auth/github/callback` compares the state with the cookie, exchanges the code, reads
   `/user`, upserts the submitter by GitHub id (login and name are refreshed), starts a session,
   and redirects to `/`. A failure is a plain-text page saying what happened.

A disabled submitter's sessions stop working at once and their sign-in is refused; their
submissions stay.

### Sessions

As edge's ([edge.md](edge.md#authentication)): the cookie (`__Host-intake_session` over https)
holds 256 random bits, the database only their sha256. `HttpOnly`, `SameSite=Strict`. A session
ends after 24 hours unused, 7 days after sign-in, on `Logout`, or when its submitter is disabled.
`last_seen_at` is rewritten at most once a minute. Signing in sweeps the submitter's ended
sessions.

## Storage

Neon Postgres, schema `intake`, owned by `intake_owner`, which runs migrations. The Worker
connects through Hyperdrive as `intake_app`, which may read and write rows and nothing else
(`intake/setup.sql`). Queries name the schema in full.

| Table | Holds |
|---|---|
| `submitters` | GitHub id (unique), login and name at the last sign-in, when disabled |
| `sessions` | sha256 of the session secret, submitter, created, last seen, expires |
| `submissions` | Id (`sub_` and 26 base32 characters), submitter, case id (unique among live ones), state, review note, round, accepted and newest revision, `category` and `description` of the newest `case.yaml` (500 characters at most) |
| `revisions` | The files as JSON (`[{path, content}]`, path order), content hash, note, terms version, license, who and when |

Migrations are SQL files in `intake/migrations/`, applied with goose (the version edge uses);
goose's table is `intake.goose_db_version`.

## Setting up and deploying

Once per deployment:

1. **Neon.** Create a project in the region nearest the maintainers; in its SQL editor, run
   `intake/setup.sql` with real passwords. Then migrate with the owner's direct (not pooled)
   connection string:
   `INTAKE_MIGRATE_URL='postgresql://intake_owner:…@…neon.tech/neondb?sslmode=require' mise run intake:migrate`.
2. **Hyperdrive.** `pnpm exec wrangler hyperdrive create intake --caching-disabled
   --connection-string='postgresql://intake_app:…@…neon.tech/neondb?sslmode=require'` (direct
   string, not pooled), and put the id it prints in `intake/wrangler.jsonc`.
3. **GitHub.** Create an OAuth app with the callback URL `https://<host>/auth/github/callback`.
4. **Turnstile.** Create a widget for the host.
5. **Worker.** Set `PUBLIC_URL`, `GITHUB_CLIENT_ID`, and `TURNSTILE_SITE_KEY` in
   `wrangler.jsonc`, then `wrangler secret put GITHUB_CLIENT_SECRET` and
   `wrangler secret put TURNSTILE_SECRET_KEY`. Route the host to the Worker.

Then each release: `mise run intake:migrate` if there are new migrations, and
`mise run intake:deploy`, which builds the pages and runs `wrangler deploy`.

## Developing

```bash
# A local Postgres, set up as on Neon; wrangler.jsonc's localConnectionString points at it.
docker run -d --name intake-pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:5432:5432 postgres:18-alpine
sed 's/change-me-owner/owner/; s/change-me-app/intake_app/' intake/setup.sql | docker exec -i intake-pg psql -U postgres
INTAKE_MIGRATE_URL='postgresql://intake_owner:owner@127.0.0.1:5432/postgres?sslmode=disable' mise run intake:migrate

# intake/.dev.vars (not committed)
PUBLIC_URL=http://127.0.0.1:8787
GITHUB_CLIENT_SECRET=…
TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA   # Turnstile's always-pass test key
TURNSTILE_SITE_KEY=1x00000000000000000000AA

mise run intake:dev          # builds the pages, then wrangler dev on http://127.0.0.1:8787
```

To work on the pages with hot reload, run `pnpm dev:intake` in `console/` (port 5174, proxying
the API and `/auth/` to the Worker) and start the Worker with `PUBLIC_URL=http://127.0.0.1:5174`,
the only origin it then accepts calls from. Signing in needs a GitHub OAuth app whose callback
is on that origin.

| Task | Does |
|---|---|
| `mise run intake:check` | `tsc`, `eslint`, and the unit tests of the checks, the credential rules, and ids. Part of `mise run check` |
| `mise run intake:test-integration` | The Worker in workerd (`@cloudflare/vitest-plugin`) against a throwaway Postgres set up by `setup.sql` and the migrations, with GitHub and Turnstile replaced by a local mock. Needs docker |
| `mise run intake:build` | The pages into `intake/public/` (not committed) |
| `mise run proto:lint:intake` | The Worker's generated code matches `proto/` |

## Tech choices

| Need | Choice | Why |
|---|---|---|
| Runtime | Cloudflare Workers, static assets | The proposer's choice (spec decision 2). One deployable serves pages and API |
| Database | Neon Postgres through Hyperdrive, `pg` | The proposer's choice. `pg` is the driver Cloudflare's Neon guide names; Neon's serverless driver would pool a second time |
| API | `@connectrpc/connect` 2.x on `createConnectRouter`, called per path with `universalServerRequestFromFetch` | The same contracts and generated code as the rest of the repo; its fetch adapters run on Workers |
| YAML | `yaml` | Already the console's; caps alias expansion |
| GitHub OAuth | Our own, in `src/github.ts` | Three requests. `arctic`, the usual library, is marked unsupported on npm |
| Turnstile | Our own siteverify call; the pages load Turnstile's script explicitly | One request; no maintained official client |
| Migrations | goose, run with `go run` | Plain SQL, the tool edge already uses |
| Tests | Vitest; `@cloudflare/vitest-plugin`; `@testcontainers/postgresql` | The successor of `@cloudflare/vitest-pool-workers`, which is deprecated; the Postgres image the other tests use |

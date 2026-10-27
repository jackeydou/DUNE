# SwarmEval console

The web console: runs, replay, the case library, analysis, and accounts, in a browser. A
single-page app that edge serves from its own binary. What each page does, how it is served,
and its security headers: [docs/services/edge.md](../docs/services/edge.md#console).

The project has a second entry, the intake site's pages (`intake/index.html`,
`src/intake/`): sign-in with GitHub, a submitter's own cases, a new submission from a template,
and a submission's files in the same flow and source views as the console's case pages. They are
built into `../intake/public/`, which the intake Worker serves; the site itself is
[docs/services/intake.md](../docs/services/intake.md).

```bash
mise run console:build      # into go/internal/edge/webui/static/; build edge after it
mise run intake:build       # the intake pages into ../intake/public/
mise run console:check      # tsc, eslint, vitest, for both entries
mise run console:e2e        # Playwright against a local stack; needs docker and a Chromium
```

## Developing

```bash
pnpm install
pnpm dev                    # http://127.0.0.1:5173, the API proxied to a local edge
```

Start edge with `--public-url http://127.0.0.1:5173`: it accepts cookie requests from that
origin only. `SWARM_EDGE` names an edge other than `http://127.0.0.1:7443`.

```bash
pnpm dev:intake             # http://127.0.0.1:5174, the intake API and /auth/ proxied to a local Worker
```

`INTAKE_WORKER` names a Worker other than `http://127.0.0.1:8787` (`wrangler dev`'s default).
The Worker must take `http://127.0.0.1:5174` as its public URL, for the same reason as edge.

## Contract

- The console's pages are a client of edge's public API (`proto/swarmeval/api/v1`) and of
  nothing else. The clients in `src/gen/` (edge's API and the intake site's) are generated
  (`mise run proto:gen`) and committed; never edit them.
- The intake pages (`src/intake/`) are a client of the intake site's API
  (`proto/swarmeval/intake/v1`) and of nothing else. They never import edge's clients, the
  console's pages, router, or session, or edge's generated API; `eslint.config.js` refuses it,
  so the public bundle carries no shape of the platform's API. They are their own TypeScript
  project (`tsconfig.intake.json`) because they register a router of their own. Their headers
  are `intake/public/_headers`, which Workers static assets apply: the console's policy, plus
  Turnstile's script and frame from `https://challenges.cloudflare.com`.
- Shared views take files structurally (`FileLike` in `src/lib/files.ts`), so the same editor,
  flow, and diff serve edge's `CaseFile` and the intake site's `SubmissionFile`.
- Run and case content is rendered as text. No `dangerouslySetInnerHTML`, and no Markdown
  rendering of anything that came from a run: trajectories hold hostile content on purpose.
- Components in `src/components/ui/` come from shadcn/ui through its CLI
  (`pnpm dlx shadcn@latest add <name>`); change them there, not by hand-writing new ones.
- No state outside the server: the session is edge's cookie, and nothing is kept in
  `localStorage` or a cookie of the app's own. The sidebar opens expanded on every load.
- The theme is tweakcn's "Claude", installed with
  `pnpm dlx shadcn@latest add https://tweakcn.com/r/themes/claude.json`, which rewrites the
  tokens in `src/index.css`. The `--success`, `--warning`, `--info`, and `--danger` tokens there
  (run and job states, errors) are the app's own; keep them when changing the theme. Fonts are
  the system's: the page's Content-Security-Policy loads none from elsewhere.

## Limits

- The replay renders every event of a run at once; past a few thousand events it is slow.
- A causal chain is read from the run's export, so it is there only once the run has ended.
- The SQL page shows at most 1,000 rows; `swarm query` returns up to 10,000.

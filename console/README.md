# SwarmEval console

The web console: runs, replay, the case library, analysis, and accounts, in a browser. A
single-page app that edge serves from its own binary. What each page does, how it is served,
and its security headers: [docs/services/edge.md](../docs/services/edge.md#console).

```bash
mise run console:build      # into go/internal/edge/webui/static/; build edge after it
mise run console:check      # tsc, eslint, vitest
mise run console:e2e        # Playwright against a local stack; needs docker and a Chromium
```

## Developing

```bash
pnpm install
pnpm dev                    # http://127.0.0.1:5173, the API proxied to a local edge
```

Start edge with `--public-url http://127.0.0.1:5173`: it accepts cookie requests from that
origin only. `SWARM_EDGE` names an edge other than `http://127.0.0.1:7443`.

## Contract

- It is a client of edge's public API (`proto/swarmeval/api/v1`) and of nothing else. The
  client in `src/gen/` is generated (`mise run proto:gen`) and committed; never edit it.
- Run and case content is rendered as text. No `dangerouslySetInnerHTML`, and no Markdown
  rendering of anything that came from a run: trajectories hold hostile content on purpose.
- Components in `src/components/ui/` come from shadcn/ui through its CLI
  (`pnpm dlx shadcn@latest add <name>`); change them there, not by hand-writing new ones.
- No state outside the server: the session is edge's cookie, and nothing is kept in
  `localStorage`.

## Limits

- The replay renders every event of a run at once; past a few thousand events it is slow.
- A causal chain is read from the run's export, so it is there only once the run has ended.
- The SQL page shows at most 1,000 rows; `swarm query` returns up to 10,000.

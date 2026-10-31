# SwarmEval intake

The public case intake site: a Cloudflare Worker where anyone with a GitHub account submits a
case for the maintainers to evaluate. It only collects cases; nothing runs here, and it has no
route into the platform. How it works, its rules, and how to set it up and deploy it:
[docs/services/intake.md](../docs/services/intake.md). Why: the
[intake spec](../spec/2026-10-25-public-case-intake/README.md).

```bash
mise run intake:check              # tsc, eslint, unit tests
mise run intake:test-integration   # the Worker in workerd against a throwaway Postgres; needs docker
mise run intake:dev                # builds the pages, then wrangler dev (needs .dev.vars and a local Postgres)
mise run intake:deploy             # builds the pages, then wrangler deploy
```

## Contract

- The API is `proto/swarmeval/intake/v1`; `src/gen/` is generated (`mise run proto:gen`) and
  committed. Never edit it. The pages are `console/src/intake/`, built into `public/`, which is
  not committed.
- The Worker holds no platform credential and calls nothing but GitHub and Turnstile. Never give
  it a binding, secret, or route that reaches the internal network: the internal side pulls.
- Submitted content is hostile. It is stored as text and rendered as text; the Worker never
  evaluates or imports it.
- The rules a submission must keep are in `src/config.ts` and `src/checks.ts`. They are this
  site's own rules, not the case loader's: do not copy loader rules here.
- Every query names the `intake` schema. Schema changes are new files in `migrations/`, applied
  with `mise run intake:migrate`; a migration never edits rows of `revisions`.
- `src/config.ts`'s `TERMS_VERSION` changes whenever the submission terms change.

## Limits

- Signing in needs a GitHub account; there is no other way in.
- The write limit counts per client address in one Cloudflare location; it is a brake, not an
  exact quota. The daily quota per submitter is exact.
- Nothing yet moves submissions to the platform or shows results (spec Plan P2 and P3).

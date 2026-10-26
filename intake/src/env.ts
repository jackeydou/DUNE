// Secrets, which `wrangler types` cannot see in wrangler.jsonc. Set them with
// `wrangler secret put`, or in .dev.vars for `wrangler dev`. Both names of the bindings' type get
// them: the Worker's `Env` and the `Cloudflare.Env` that `cloudflare:test` hands to tests.
interface Secrets {
  GITHUB_CLIENT_SECRET: string
  TURNSTILE_SECRET_KEY: string
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface Env extends Secrets {}
  // Augments the namespace `wrangler types` declares; there is no module form of it.
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Cloudflare {
    // eslint-disable-next-line @typescript-eslint/no-empty-object-type
    interface Env extends Secrets {}
  }
}

export {}

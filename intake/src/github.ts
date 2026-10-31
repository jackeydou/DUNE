// Signing in with GitHub: the OAuth web flow, with no scopes, so the site reads only a user's
// public profile. GitHub's token is used once, to read who signed in, and never stored.
// Behavior: docs/services/intake.md#signing-in.
import { withDb } from "./db"
import { newSecret, sameSecret } from "./ids"
import { cookie, startSession, stateCookie, readCookie } from "./session"

const STATE_MAX_AGE_SECONDS = 10 * 60

const callbackUrl = (env: Env) => new URL("/auth/github/callback", env.PUBLIC_URL).toString()

/** `/auth/github/start`: off to GitHub, with a state that only this browser holds. The state
 * cookie is `Lax` because the browser comes back from github.com. */
export function startSignIn(env: Env): Response {
  const state = newSecret()
  const to = new URL("/login/oauth/authorize", env.GITHUB_WEB_URL)
  to.searchParams.set("client_id", env.GITHUB_CLIENT_ID)
  to.searchParams.set("redirect_uri", callbackUrl(env))
  to.searchParams.set("state", state)
  to.searchParams.set("allow_signup", "true")
  return new Response(null, {
    status: 303,
    headers: { Location: to.toString(), "Set-Cookie": cookie(env, stateCookie(env), state, STATE_MAX_AGE_SECONDS, "Lax") },
  })
}

/** `/auth/github/callback`: checks the state, asks GitHub who signed in, and starts a session. */
export async function finishSignIn(env: Env, request: Request): Promise<Response> {
  const url = new URL(request.url)
  const code = url.searchParams.get("code")
  const state = url.searchParams.get("state")
  const expected = readCookie(request.headers.get("Cookie"), stateCookie(env))
  const clearState = cookie(env, stateCookie(env), "", 0, "Lax")
  if (url.searchParams.has("error"))
    return page(400, "GitHub did not sign you in", `GitHub answered: ${url.searchParams.get("error_description") ?? url.searchParams.get("error")}.`, clearState)
  if (!code || !state || !expected || !sameSecret(state, expected))
    return page(400, "Sign-in expired", "The sign-in did not start in this browser, or took longer than ten minutes.", clearState)

  const user = await githubUser(env, await accessToken(env, code))
  const signedIn = await withDb(env, async (db) => {
    const { rows } = await db.query<{ submitter_id: string; disabled: boolean }>(
      `INSERT INTO intake.submitters (github_id, github_login, name) VALUES ($1, $2, $3)
       ON CONFLICT (github_id) DO UPDATE
         SET github_login = EXCLUDED.github_login, name = EXCLUDED.name, last_login_at = now()
       RETURNING submitter_id, disabled_at IS NOT NULL AS disabled`,
      [user.id, user.login, user.name]
    )
    const row = rows[0]!
    if (row.disabled) return undefined
    return startSession(env, db, row.submitter_id)
  })
  if (!signedIn) return page(403, "Account disabled", "This GitHub account may no longer submit cases.", clearState)
  const headers = new Headers({ Location: "/" })
  headers.append("Set-Cookie", signedIn)
  headers.append("Set-Cookie", clearState)
  return new Response(null, { status: 303, headers })
}

async function accessToken(env: Env, code: string): Promise<string> {
  const res = await fetch(new URL("/login/oauth/access_token", env.GITHUB_WEB_URL), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code, redirect_uri: callbackUrl(env) }),
  })
  if (!res.ok) throw new Error(`GitHub's token endpoint answered HTTP ${res.status}`)
  const body: unknown = await res.json()
  if (typeof body === "object" && body !== null && "access_token" in body && typeof body.access_token === "string") return body.access_token
  // An expired or reused code is GitHub's `bad_verification_code`, with HTTP 200.
  throw new SignInError(`GitHub refused the sign-in code: ${JSON.stringify(body)}`)
}

interface GitHubUser {
  id: string
  login: string
  name: string
}

async function githubUser(env: Env, token: string): Promise<GitHubUser> {
  const res = await fetch(new URL("/user", env.GITHUB_API_URL), {
    headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${token}`, "User-Agent": "swarmeval-intake" },
  })
  if (!res.ok) throw new Error(`GitHub's /user answered HTTP ${res.status}`)
  const body: unknown = await res.json()
  if (
    typeof body !== "object" || body === null ||
    !("id" in body) || typeof body.id !== "number" || !Number.isSafeInteger(body.id) ||
    !("login" in body) || typeof body.login !== "string"
  )
    throw new Error(`GitHub's /user answered without a numeric id and a login: ${JSON.stringify(body)}`)
  const name = "name" in body && typeof body.name === "string" ? body.name : ""
  return { id: String(body.id), login: body.login, name }
}

/** A sign-in GitHub turned down, as opposed to GitHub failing: shown to the person, not logged as
 * a fault. */
export class SignInError extends Error {}

/** A plain text page for a sign-in that did not finish. Text, so nothing GitHub sent back is ever
 * parsed as HTML. */
export function page(status: number, title: string, detail: string, setCookie?: string): Response {
  const headers = new Headers({ "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" })
  if (setCookie) headers.append("Set-Cookie", setCookie)
  return new Response(`${title}\n\n${detail}\n\nStart again: /auth/github/start\n`, { status, headers })
}

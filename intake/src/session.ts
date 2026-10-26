// Browser sessions, as edge keeps them: the cookie holds a random secret, the database only its
// sha256. Behavior: docs/services/intake.md#sessions.
import { Code, ConnectError } from "@connectrpc/connect"

import { SESSION_IDLE_MS, SESSION_MAX_AGE_MS, SESSION_TOUCH_MS } from "./config"
import type { Db } from "./db"
import { newSecret, sha256 } from "./ids"

export interface Submitter {
  submitterId: string
  githubId: string
  githubLogin: string
  name: string
}

/** Whether the site is served over https, which decides the cookies' `Secure` and `__Host-`. */
const secure = (env: Env) => env.PUBLIC_URL.startsWith("https://")

export const sessionCookie = (env: Env) => (secure(env) ? "__Host-intake_session" : "intake_session")
export const stateCookie = (env: Env) => (secure(env) ? "__Host-intake_oauth_state" : "intake_oauth_state")

export function readCookie(header: string | null, name: string): string | undefined {
  for (const part of (header ?? "").split(";")) {
    const at = part.indexOf("=")
    if (at > 0 && part.slice(0, at).trim() === name) return part.slice(at + 1).trim()
  }
  return undefined
}

/** A Set-Cookie value. `maxAgeSeconds` 0 clears the cookie. */
export function cookie(env: Env, name: string, value: string, maxAgeSeconds: number, sameSite: "Strict" | "Lax"): string {
  return `${name}=${value}; Path=/; HttpOnly; SameSite=${sameSite}; Max-Age=${maxAgeSeconds}${secure(env) ? "; Secure" : ""}`
}

/** Starts a session for the submitter and returns its Set-Cookie value. */
export async function startSession(env: Env, db: Db, submitterId: string): Promise<string> {
  const secret = newSecret()
  await db.query(
    `INSERT INTO intake.sessions (session_hash, submitter_id, expires_at)
     VALUES ($1, $2, now() + make_interval(secs => $3))`,
    [await sha256(secret), submitterId, SESSION_MAX_AGE_MS / 1000]
  )
  // Signing in is also when ended sessions are swept, as edge does.
  await db.query("DELETE FROM intake.sessions WHERE submitter_id = $1 AND expires_at < now()", [submitterId])
  return cookie(env, sessionCookie(env), secret, SESSION_MAX_AGE_MS / 1000, "Strict")
}

/** The signed-in submitter, or UNAUTHENTICATED. A session ends when unused for the idle period,
 * at its expiry, and when its submitter is disabled. */
export async function requireSubmitter(env: Env, db: Db, cookieHeader: string | null): Promise<Submitter> {
  const secret = readCookie(cookieHeader, sessionCookie(env))
  if (!secret) throw new ConnectError("Sign in with GitHub first.", Code.Unauthenticated)
  const hash = await sha256(secret)
  const { rows } = await db.query<{ submitter_id: string; github_id: string; github_login: string; name: string; touch: boolean }>(
    `SELECT s.submitter_id, u.github_id, u.github_login, u.name,
            s.last_seen_at < now() - make_interval(secs => $3) AS touch
       FROM intake.sessions s JOIN intake.submitters u USING (submitter_id)
      WHERE s.session_hash = $1 AND s.expires_at > now()
        AND s.last_seen_at > now() - make_interval(secs => $2) AND u.disabled_at IS NULL`,
    [hash, SESSION_IDLE_MS / 1000, SESSION_TOUCH_MS / 1000]
  )
  const row = rows[0]
  if (!row) throw new ConnectError("Your session has ended. Sign in with GitHub again.", Code.Unauthenticated)
  if (row.touch) await db.query("UPDATE intake.sessions SET last_seen_at = now() WHERE session_hash = $1", [hash])
  return { submitterId: row.submitter_id, githubId: row.github_id, githubLogin: row.github_login, name: row.name }
}

/** Ends the session the cookie names, if any. */
export async function endSession(env: Env, db: Db, cookieHeader: string | null): Promise<void> {
  const secret = readCookie(cookieHeader, sessionCookie(env))
  if (secret) await db.query("DELETE FROM intake.sessions WHERE session_hash = $1", [await sha256(secret)])
}

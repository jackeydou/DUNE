// Turnstile's server-side check of a widget response (siteverify). A response is good once.
import { Code, ConnectError } from "@connectrpc/connect"

/** Refuses the call unless Turnstile vouches for `token`, issued for this site's host name. */
export async function verifyTurnstile(env: Env, token: string, clientIp: string): Promise<void> {
  if (!token) throw new ConnectError("Complete the human check before saving.", Code.PermissionDenied)
  const body = new URLSearchParams({ secret: env.TURNSTILE_SECRET_KEY, response: token, remoteip: clientIp })
  const res = await fetch(env.TURNSTILE_VERIFY_URL, { method: "POST", body })
  if (!res.ok) throw new Error(`Turnstile siteverify answered HTTP ${res.status}`)
  const outcome: unknown = await res.json()
  if (typeof outcome !== "object" || outcome === null || !("success" in outcome))
    throw new Error(`Turnstile siteverify answered without "success": ${JSON.stringify(outcome)}`)
  const hostname = "hostname" in outcome ? outcome.hostname : undefined
  if (outcome.success !== true || hostname !== new URL(env.PUBLIC_URL).hostname)
    throw new ConnectError("The human check failed or expired. Complete it again, then save.", Code.PermissionDenied)
}

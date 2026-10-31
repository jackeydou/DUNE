// What a call's implementation needs from the HTTP request, handed over as a context value.
import { Code, ConnectError, createContextKey, type HandlerContext } from "@connectrpc/connect"

export interface Call {
  env: Env
  /** The client's address, as Cloudflare saw it. */
  clientIp: string
  cookie: string | null
}

export const CALL = createContextKey<Call | undefined>(undefined, { description: "the intake call" })

export function callOf(context: HandlerContext): Call {
  const call = context.values.get(CALL)
  if (!call) throw new Error("the fetch handler did not set the call context")
  return call
}

/** Refuses the call with RESOURCE_EXHAUSTED once the client's address has made too many writes
 * this minute (the WRITE_LIMIT binding). */
export async function limitWrites(call: Call): Promise<void> {
  const { success } = await call.env.WRITE_LIMIT.limit({ key: call.clientIp })
  if (!success) throw new ConnectError("Too many requests from your address. Wait a minute, then try again.", Code.ResourceExhausted)
}

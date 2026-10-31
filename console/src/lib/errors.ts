// What a failed Connect call says, for any client of the console's or the intake site's API.
import { Code, ConnectError } from "@connectrpc/connect"

/** The server's own message for a failed call, without Connect's `[code]` prefix. */
export function errorText(err: unknown): string {
  if (err instanceof ConnectError) return err.rawMessage
  return err instanceof Error ? err.message : String(err)
}

export function isCode(err: unknown, code: Code): boolean {
  return err instanceof ConnectError && err.code === code
}

export { Code }

// The console's clients of edge's public API. Same origin, Connect's JSON protocol, and the
// session cookie the browser attaches by itself.
import { Code, ConnectError, createClient } from "@connectrpc/connect"
import { createConnectTransport } from "@connectrpc/connect-web"
import { timestampDate, type Timestamp } from "@bufbuild/protobuf/wkt"

import { AnalysisService } from "@/gen/swarmeval/api/v1/analysis_pb"
import { AuthService } from "@/gen/swarmeval/api/v1/auth_pb"
import { CaseService } from "@/gen/swarmeval/api/v1/case_pb"
import { RunService } from "@/gen/swarmeval/api/v1/run_pb"
import { UserService } from "@/gen/swarmeval/api/v1/user_pb"

const transport = createConnectTransport({ baseUrl: window.location.origin })

export const auth = createClient(AuthService, transport)
export const users = createClient(UserService, transport)
export const runs = createClient(RunService, transport)
export const cases = createClient(CaseService, transport)
export const analysis = createClient(AnalysisService, transport)

/** edge's own message for a failed call, without Connect's `[code]` prefix. */
export function errorText(err: unknown): string {
  if (err instanceof ConnectError) return err.rawMessage
  return err instanceof Error ? err.message : String(err)
}

export function isCode(err: unknown, code: Code): boolean {
  return err instanceof ConnectError && err.code === code
}

export { Code }

export function when(ts: Timestamp | undefined): string {
  return ts ? timestampDate(ts).toLocaleString() : "—"
}

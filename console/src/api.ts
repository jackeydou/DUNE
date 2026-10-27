// The console's clients of edge's public API. Same origin, Connect's JSON protocol, and the
// session cookie the browser attaches by itself.
import { createClient } from "@connectrpc/connect"
import { createConnectTransport } from "@connectrpc/connect-web"

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

export { Code, errorText, isCode } from "@/lib/errors"
export { when } from "@/lib/time"

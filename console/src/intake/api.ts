// The intake pages' clients of the intake Worker's API (proto/swarmeval/intake/v1). Same origin,
// Connect's JSON protocol, and the session cookie the browser attaches by itself. Nothing here
// reaches edge: the intake site is a deployment of its own.
import { createClient } from "@connectrpc/connect"
import { createConnectTransport } from "@connectrpc/connect-web"

import { IntakeAuthService } from "@/gen/swarmeval/intake/v1/auth_pb"
import { SiteService } from "@/gen/swarmeval/intake/v1/site_pb"
import { SubmissionService } from "@/gen/swarmeval/intake/v1/submission_pb"

const transport = createConnectTransport({ baseUrl: window.location.origin })

export const site = createClient(SiteService, transport)
export const auth = createClient(IntakeAuthService, transport)
export const submissions = createClient(SubmissionService, transport)

/** Where signing in starts: a page navigation into GitHub's OAuth flow, not a call. */
export const SIGN_IN_PATH = "/auth/github/start"

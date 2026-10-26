// The intake Worker: the intake API over Connect's JSON protocol, GitHub sign-in, and the pages
// (static assets, served by Cloudflare before this code runs for any other path).
import { Code, ConnectError, createConnectRouter, createContextValues, type Interceptor } from "@connectrpc/connect"
import { universalServerRequestFromFetch, universalServerResponseToFetch } from "@connectrpc/connect/protocol"

import "./env"
import { auth, site } from "./account"
import { CALL, limitWrites } from "./call"
import { IntakeAuthService } from "./gen/swarmeval/intake/v1/auth_pb"
import { SiteService } from "./gen/swarmeval/intake/v1/site_pb"
import { SubmissionService } from "./gen/swarmeval/intake/v1/submission_pb"
import { finishSignIn, page, SignInError, startSignIn } from "./github"
import { cookie, stateCookie } from "./session"
import { submissions } from "./submissions"

/** A request body of 1 MiB of files, base64 in JSON, with room to spare. */
const READ_MAX_BYTES = 2 * 1024 * 1024

/** Errors that are not the caller's go back as INTERNAL with nothing of their cause, which is
 * logged instead: a database or GitHub failure must not show the site's insides. */
const hideInternals: Interceptor = (next) => async (req) => {
  try {
    return await next(req)
  } catch (err) {
    if (err instanceof ConnectError) throw err
    console.error(`${req.method.parent.typeName}.${req.method.name} failed`, err)
    throw new ConnectError("The intake site failed to answer. Try again; if it keeps failing, tell the maintainers.", Code.Internal)
  }
}

const router = createConnectRouter({ grpc: false, grpcWeb: false, readMaxBytes: READ_MAX_BYTES, interceptors: [hideInternals] })
  .service(SiteService, site)
  .service(IntakeAuthService, auth)
  .service(SubmissionService, submissions)

const handlers = new Map(router.handlers.map((h) => [h.requestPath, h]))

/** One call, as Connect's createFetchHandler would make it, with the call context added. */
async function rpc(request: Request, env: Env, path: string): Promise<Response> {
  const handler = handlers.get(path)
  if (!handler) return new Response("Not found", { status: 404 })
  // Every call is a POST from the site's own pages: a request from another origin, or with no
  // Origin, is refused before it reaches the API, so another site cannot ride the cookie.
  if (request.headers.get("Origin") !== new URL(env.PUBLIC_URL).origin)
    return Response.json({ code: "permission_denied", message: `Requests are accepted from ${new URL(env.PUBLIC_URL).origin} only.` }, { status: 403 })
  const uReq = universalServerRequestFromFetch(request, {})
  const values = createContextValues()
  values.set(CALL, { env, clientIp: request.headers.get("CF-Connecting-IP") ?? "", cookie: request.headers.get("Cookie") })
  const uRes = await handler({ ...uReq, contextValues: values })
  const res = universalServerResponseToFetch(uRes)
  res.headers.set("Cache-Control", "no-store")
  return res
}

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url)
    if (url.pathname.startsWith("/swarmeval.")) return rpc(request, env, url.pathname)
    if (url.pathname === "/auth/github/start" && request.method === "GET") {
      const clientIp = request.headers.get("CF-Connecting-IP") ?? ""
      try {
        await limitWrites({ env, clientIp, cookie: null })
      } catch (err) {
        if (err instanceof ConnectError) return page(429, "Too many sign-ins", err.rawMessage)
        throw err
      }
      return startSignIn(env)
    }
    if (url.pathname === "/auth/github/callback" && request.method === "GET") {
      const clearState = cookie(env, stateCookie(env), "", 0, "Lax")
      try {
        return await finishSignIn(env, request)
      } catch (err) {
        if (err instanceof SignInError)
          return page(400, "GitHub did not sign you in", "The sign-in code was refused, most likely because it was used or expired.", clearState)
        console.error("GitHub sign-in failed", err)
        return page(502, "Sign-in failed", "GitHub could not be reached or answered something unexpected. Try again in a minute.", clearState)
      }
    }
    return env.ASSETS.fetch(request)
  },
} satisfies ExportedHandler<Env>

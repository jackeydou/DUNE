// SiteService and IntakeAuthService.
import type { HandlerContext, ServiceImpl } from "@connectrpc/connect"

import { callOf } from "./call"
import { ALLOWED_IMAGES, LICENSE, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, TERMS_VERSION, WORKSPACE } from "./config"
import { withDb } from "./db"
import type { IntakeAuthService } from "./gen/swarmeval/intake/v1/auth_pb"
import type { SiteService } from "./gen/swarmeval/intake/v1/site_pb"
import { cookie, endSession, requireSubmitter, sessionCookie } from "./session"

export const site: ServiceImpl<typeof SiteService> = {
  getSiteConfig(_req, context: HandlerContext) {
    return {
      turnstileSiteKey: callOf(context).env.TURNSTILE_SITE_KEY,
      termsVersion: TERMS_VERSION,
      license: LICENSE,
      allowedImages: [...ALLOWED_IMAGES],
      workspace: WORKSPACE,
      maxFiles: MAX_FILES,
      maxFileBytes: MAX_FILE_BYTES,
      maxTotalBytes: MAX_TOTAL_BYTES,
    }
  },
}

export const auth: ServiceImpl<typeof IntakeAuthService> = {
  async whoAmI(_req, context) {
    const { env, cookie: header } = callOf(context)
    const me = await withDb(env, (db) => requireSubmitter(env, db, header))
    return { submitter: { githubLogin: me.githubLogin, githubId: BigInt(me.githubId), name: me.name } }
  },
  async logout(_req, context) {
    const { env, cookie: header } = callOf(context)
    await withDb(env, (db) => endSession(env, db, header))
    context.responseHeader.append("Set-Cookie", cookie(env, sessionCookie(env), "", 0, "Strict"))
    return {}
  },
}

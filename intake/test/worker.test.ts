// The Worker end to end in workerd: GitHub sign-in against the mock, the API over Connect's JSON
// protocol, and the rows it leaves in Postgres.
import { Code, ConnectError, createClient, type Client, type Interceptor } from "@connectrpc/connect"
import { createConnectTransport } from "@connectrpc/connect-web"
import { env, SELF } from "cloudflare:test"
import type { QueryResultRow } from "pg"
import { beforeAll, describe, expect, it } from "vitest"

import { TERMS_VERSION } from "../src/config"
import { withDb } from "../src/db"
import { IntakeAuthService } from "../src/gen/swarmeval/intake/v1/auth_pb"
import { SiteService } from "../src/gen/swarmeval/intake/v1/site_pb"
import { ReviewState, SubmissionService } from "../src/gen/swarmeval/intake/v1/submission_pb"

const ORIGIN = "https://intake.test"
const enc = new TextEncoder()
let addresses = 0

interface Session {
  cookie: string
  auth: Client<typeof IntakeAuthService>
  submissions: Client<typeof SubmissionService>
}

/** Clients that call the Worker as the site's pages do, from a client address of their own so
 * the write limit of one test does not reach another. */
function clients(cookie: string, ip = `198.51.100.${++addresses}`) {
  const headers: Interceptor = (next) => (req) => {
    req.header.set("Origin", ORIGIN)
    req.header.set("CF-Connecting-IP", ip)
    if (cookie) req.header.set("Cookie", cookie)
    return next(req)
  }
  const transport = createConnectTransport({
    baseUrl: ORIGIN,
    // connect-web asks for redirect "error", which workerd does not implement.
    fetch: (input, init) => SELF.fetch(new Request(input, { ...init, redirect: "manual" })),
    interceptors: [headers],
  })
  return {
    site: createClient(SiteService, transport),
    auth: createClient(IntakeAuthService, transport),
    submissions: createClient(SubmissionService, transport),
  }
}

const nameValue = (setCookie: string) => setCookie.split(";")[0]!

/** Signs in through the mock GitHub as `login` and returns that browser's session. */
async function signIn(login: string): Promise<Session> {
  const start = await SELF.fetch(`${ORIGIN}/auth/github/start`, { redirect: "manual" })
  expect(start.status).toBe(303)
  const state = new URL(start.headers.get("Location")!).searchParams.get("state")!
  const back = await SELF.fetch(`${ORIGIN}/auth/github/callback?code=good-${login}&state=${state}`, {
    redirect: "manual",
    headers: { Cookie: nameValue(start.headers.get("Set-Cookie")!) },
  })
  expect(back.status).toBe(303)
  expect(back.headers.get("Location")).toBe("/")
  const session = back.headers.getSetCookie().find((c) => c.startsWith("__Host-intake_session="))!
  expect(session).toContain("HttpOnly")
  expect(session).toContain("SameSite=Strict")
  expect(session).toContain("Secure")
  const cookie = nameValue(session)
  return { cookie, ...clients(cookie) }
}

const caseYaml = (id: string, extra = "") => `schema_version: 5
id: ${id}
workspace: community
category: honesty
description: Two agents and a promise.
swarm:
  agents:
    - { id: a, prompt: prompts/a.md, tools: [send_message], sandbox: none }
    - { id: b, prompt: prompts/b.md, tools: [send_message], sandbox: none }
  channels:
    - { id: chat, members: [a, b] }
task:
  input: prompts/task.md
${extra}`

const files = (id: string, extra = "") =>
  Object.entries({
    "case.yaml": caseYaml(id, extra),
    "README.md": "# A case\n",
    "prompts/a.md": "You are A.\n",
    "prompts/b.md": "You are B.\n",
    "prompts/task.md": "Begin.\n",
  }).map(([path, content]) => ({ path, content: enc.encode(content) }))

const save = { acceptedTerms: TERMS_VERSION, turnstileToken: "pass" }

async function refused(call: Promise<unknown>, code: Code): Promise<string> {
  try {
    await call
  } catch (err) {
    expect(err).toBeInstanceOf(ConnectError)
    expect(Code[(err as ConnectError).code]).toBe(Code[code])
    return (err as ConnectError).rawMessage
  }
  throw new Error(`the call succeeded; expected ${Code[code]}`)
}

async function sql<T extends QueryResultRow>(text: string, values: unknown[] = []): Promise<T[]> {
  return withDb(env, async (db) => (await db.query<T>(text, values)).rows)
}

describe("without a session", () => {
  it("serves the site's rules", async () => {
    const config = await clients("").site.getSiteConfig({})
    expect(config.termsVersion).toBe(TERMS_VERSION)
    expect(config.license).toBe("CC-BY-4.0")
    expect(config.workspace).toBe("community")
    expect(config.turnstileSiteKey).toBe("test-site-key")
    expect(config.allowedImages).toContain("python:3.12-slim")
  })

  it("refuses everything else as UNAUTHENTICATED", async () => {
    const { auth, submissions } = clients("")
    await refused(auth.whoAmI({}), Code.Unauthenticated)
    await refused(submissions.listMySubmissions({}), Code.Unauthenticated)
    await refused(submissions.checkFiles({ files: files("x") }), Code.Unauthenticated)
  })

  it("refuses a call from another origin, or with none", async () => {
    const url = `${ORIGIN}/swarmeval.intake.v1.SiteService/GetSiteConfig`
    const init = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
    expect((await SELF.fetch(url, init)).status).toBe(403)
    expect((await SELF.fetch(url, { ...init, headers: { ...init.headers, Origin: "https://evil.test" } })).status).toBe(403)
  })
})

describe("signing in", () => {
  it("starts a session and keeps GitHub's login", async () => {
    const alice = await signIn("alice")
    const { submitter } = await alice.auth.whoAmI({})
    expect(submitter?.githubLogin).toBe("alice")
    expect(submitter?.name).toBe("alice name")
  })

  it("refuses a callback whose state this browser did not start", async () => {
    const res = await SELF.fetch(`${ORIGIN}/auth/github/callback?code=good-mallory&state=forged`, {
      redirect: "manual",
      headers: { Cookie: "__Host-intake_oauth_state=other" },
    })
    expect(res.status).toBe(400)
    expect(await res.text()).toContain("Sign-in expired")
  })

  it("refuses a code GitHub refuses", async () => {
    const start = await SELF.fetch(`${ORIGIN}/auth/github/start`, { redirect: "manual" })
    const state = new URL(start.headers.get("Location")!).searchParams.get("state")!
    const res = await SELF.fetch(`${ORIGIN}/auth/github/callback?code=used&state=${state}`, {
      redirect: "manual",
      headers: { Cookie: nameValue(start.headers.get("Set-Cookie")!) },
    })
    expect(res.status).toBe(400)
    const cookies = res.headers.getSetCookie()
    expect(cookies.some((c) => c.startsWith("__Host-intake_oauth_state=;"))).toBe(true)
    expect(cookies.some((c) => c.includes("intake_session"))).toBe(false)
  })

  it("ends the session on logout", async () => {
    const bob = await signIn("bob")
    await bob.auth.logout({})
    await refused(bob.auth.whoAmI({}), Code.Unauthenticated)
  })

  it("ends every session of a disabled submitter and refuses their sign-in", async () => {
    const carol = await signIn("carol")
    await sql("UPDATE intake.submitters SET disabled_at = now() WHERE github_login = 'carol'")
    await refused(carol.auth.whoAmI({}), Code.Unauthenticated)
    const start = await SELF.fetch(`${ORIGIN}/auth/github/start`, { redirect: "manual" })
    const state = new URL(start.headers.get("Location")!).searchParams.get("state")!
    const res = await SELF.fetch(`${ORIGIN}/auth/github/callback?code=good-carol&state=${state}`, {
      redirect: "manual",
      headers: { Cookie: nameValue(start.headers.get("Set-Cookie")!) },
    })
    expect(res.status).toBe(403)
  })
})

describe("submissions", () => {
  let dana: Session
  let erin: Session
  beforeAll(async () => {
    dana = await signIn("dana")
    erin = await signIn("erin")
  })

  it("saves a case as revision 1, waiting for the maintainers", async () => {
    const { submission, revision } = await dana.submissions.createSubmission({ files: files("dana_first"), note: "first", ...save })
    expect(submission?.submissionId).toMatch(/^sub_[a-z2-7]{26}$/)
    expect(submission?.caseId).toBe("dana_first")
    expect(submission?.category).toBe("honesty")
    expect(submission?.state).toBe(ReviewState.SUBMITTED)
    expect(revision?.revision).toBe(1)
    expect(revision?.termsVersion).toBe(TERMS_VERSION)
    expect(revision?.license).toBe("CC-BY-4.0")
    const got = await dana.submissions.getSubmissionRevision({ submissionId: submission!.submissionId, revision: 0 })
    expect(got.files.map((f) => f.path)).toEqual(["README.md", "case.yaml", "prompts/a.md", "prompts/b.md", "prompts/task.md"])
    expect(new TextDecoder().decode(got.files[0]!.content)).toBe("# A case\n")
  })

  it("needs the human check and the current terms", async () => {
    await refused(dana.submissions.createSubmission({ files: files("dana_check"), ...save, turnstileToken: "" }), Code.PermissionDenied)
    await refused(dana.submissions.createSubmission({ files: files("dana_check"), ...save, turnstileToken: "fail" }), Code.PermissionDenied)
    const msg = await refused(dana.submissions.createSubmission({ files: files("dana_check"), ...save, acceptedTerms: "2000-01-01" }), Code.FailedPrecondition)
    expect(msg).toContain(TERMS_VERSION)
  })

  it("refuses files that fail a check, saying every problem", async () => {
    const bad = files("dana_bad", "extensions:\n  - use: case:extensions/x.py\n").concat({ path: "../up.md", content: enc.encode("x") })
    const msg = await refused(dana.submissions.createSubmission({ files: bad, ...save }), Code.InvalidArgument)
    expect(msg).toContain("loads Python from the case")
    expect(msg).toContain("../up.md: Use a relative path")
    expect((await dana.submissions.checkFiles({ files: bad })).problems).toHaveLength(2)
    expect((await dana.submissions.checkFiles({ files: files("dana_ok") })).problems).toEqual([])
  })

  it("gives a case id to one live submission, and frees it on withdrawal", async () => {
    const { submission } = await dana.submissions.createSubmission({ files: files("shared_id"), ...save })
    const msg = await refused(erin.submissions.createSubmission({ files: files("shared_id"), ...save }), Code.AlreadyExists)
    expect(msg).toContain("shared_id")
    const withdrawn = await dana.submissions.withdrawSubmission({ submissionId: submission!.submissionId })
    expect(withdrawn.submission?.state).toBe(ReviewState.WITHDRAWN)
    await erin.submissions.createSubmission({ files: files("shared_id"), ...save })
    await refused(
      dana.submissions.updateSubmission({ submissionId: submission!.submissionId, baseRevision: 1, changes: [{ path: "README.md", change: { case: "delete", value: true } }], ...save }),
      Code.FailedPrecondition
    )
  })

  it("shows a submitter only their own", async () => {
    const { submission } = await erin.submissions.createSubmission({ files: files("erin_private"), ...save })
    await refused(dana.submissions.getSubmission({ submissionId: submission!.submissionId }), Code.NotFound)
    await refused(dana.submissions.getSubmissionRevision({ submissionId: submission!.submissionId, revision: 1 }), Code.NotFound)
    await refused(dana.submissions.withdrawSubmission({ submissionId: submission!.submissionId }), Code.NotFound)
    const mine = await dana.submissions.listMySubmissions({})
    expect(mine.submissions.map((s) => s.caseId)).not.toContain("erin_private")
  })

  it("saves changes as the next revision, against the newest only", async () => {
    const { submission } = await dana.submissions.createSubmission({ files: files("dana_edit"), ...save })
    const id = submission!.submissionId
    const readme = (text: string) => [{ path: "README.md", change: { case: "content" as const, value: enc.encode(text) } }]

    const same = await dana.submissions.updateSubmission({ submissionId: id, baseRevision: 1, changes: readme("# A case\n"), ...save, turnstileToken: "" })
    expect(same.created).toBe(false)
    expect(same.revision?.revision).toBe(1)

    await sql("UPDATE intake.submissions SET state = 'rejected', review_note = 'too short' WHERE submission_id = $1", [id])
    const next = await dana.submissions.updateSubmission({ submissionId: id, baseRevision: 1, changes: readme("# A longer case\n"), note: "longer", ...save })
    expect(next.created).toBe(true)
    expect(next.revision?.revision).toBe(2)
    expect(next.submission?.state).toBe(ReviewState.SUBMITTED)
    expect(next.submission?.reviewNote).toBe("")

    const stale = await refused(dana.submissions.updateSubmission({ submissionId: id, baseRevision: 1, changes: readme("# Late\n"), ...save }), Code.Aborted)
    expect(stale).toContain("revision 2 is the newest")
    await refused(
      dana.submissions.updateSubmission({ submissionId: id, baseRevision: 2, changes: [{ path: "nope.md", change: { case: "delete", value: true } }], ...save }),
      Code.InvalidArgument
    )
    const renamed = [{ path: "case.yaml", change: { case: "content" as const, value: enc.encode(caseYaml("dana_other")) } }]
    expect(await refused(dana.submissions.updateSubmission({ submissionId: id, baseRevision: 2, changes: renamed, ...save }), Code.InvalidArgument)).toContain(
      "must keep `id: dana_edit`"
    )

    const { revisions } = await dana.submissions.getSubmission({ submissionId: id })
    expect(revisions.map((r) => [r.revision, r.note])).toEqual([
      [2, "longer"],
      [1, ""],
    ])
    const rows = await sql<{ n: number }>("SELECT count(*)::int AS n FROM intake.revisions WHERE submission_id = $1", [id])
    expect(rows[0]!.n).toBe(2)
  })

  it("limits writes per client address", async () => {
    const { submissions } = clients(dana.cookie, "203.0.113.200")
    const outcomes = await Promise.allSettled(Array.from({ length: 31 }, () => submissions.checkFiles({ files: files("x") })))
    const limited = outcomes.filter((o) => o.status === "rejected" && o.reason instanceof ConnectError && o.reason.code === Code.ResourceExhausted)
    expect(limited.length).toBeGreaterThan(0)
  })
})

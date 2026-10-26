// SubmissionService: a signed-in submitter's own cases. Behavior: docs/services/intake.md#submissions.
import { timestampFromDate } from "@bufbuild/protobuf/wkt"
import { Code, ConnectError, type HandlerContext, type ServiceImpl } from "@connectrpc/connect"

import { callOf, limitWrites, type Call } from "./call"
import { checkFiles, contentSha256, type Checked, type InputFile, type TextFile } from "./checks"
import { LICENSE, MAX_NEW_SUBMISSIONS_PER_DAY, MAX_NOTE_CHARS, MAX_REVISIONS_PER_DAY, TERMS_VERSION } from "./config"
import { isUniqueViolation, tx, withDb, type Db } from "./db"
import { ReviewState, type FileChange, type SubmissionService } from "./gen/swarmeval/intake/v1/submission_pb"
import { newSubmissionId } from "./ids"
import { requireSubmitter, type Submitter } from "./session"
import { verifyTurnstile } from "./turnstile"

const STATES: Record<string, ReviewState> = {
  submitted: ReviewState.SUBMITTED,
  triaged: ReviewState.TRIAGED,
  accepted: ReviewState.ACCEPTED,
  rejected: ReviewState.REJECTED,
  withdrawn: ReviewState.WITHDRAWN,
}

interface SubmissionRow {
  submission_id: string
  case_id: string
  category: string
  description: string
  state: string
  review_note: string
  round: string
  accepted_revision: number
  newest_revision: number
  created_at: Date
  updated_at: Date
}

interface RevisionRow {
  revision: number
  note: string
  content_sha256: string
  terms_version: string
  license: string
  created_at: Date
}

const SUBMISSION_COLUMNS = `submission_id, case_id, category, description, state, review_note, round,
  accepted_revision, newest_revision, created_at, updated_at`
const REVISION_COLUMNS = "revision, note, content_sha256, terms_version, license, created_at"

const submission = (row: SubmissionRow) => ({
  submissionId: row.submission_id,
  caseId: row.case_id,
  category: row.category,
  description: row.description,
  state: STATES[row.state] ?? ReviewState.UNSPECIFIED,
  reviewNote: row.review_note,
  round: row.round,
  acceptedRevision: row.accepted_revision,
  newestRevision: row.newest_revision,
  createdAt: timestampFromDate(row.created_at),
  updatedAt: timestampFromDate(row.updated_at),
})

const revision = (row: RevisionRow) => ({
  revision: row.revision,
  note: row.note,
  contentSha256: row.content_sha256,
  termsVersion: row.terms_version,
  license: row.license,
  createdAt: timestampFromDate(row.created_at),
})

const encoder = new TextEncoder()

async function signedIn(context: HandlerContext): Promise<{ call: Call; me: Submitter }> {
  const call = callOf(context)
  const me = await withDb(call.env, (db) => requireSubmitter(call.env, db, call.cookie))
  return { call, me }
}

/** The submitter's own submission, or NOT_FOUND: another's is indistinguishable from none. */
async function ownSubmission(db: Db, me: Submitter, submissionId: string, lock = false): Promise<SubmissionRow> {
  const { rows } = await db.query<SubmissionRow>(
    `SELECT ${SUBMISSION_COLUMNS} FROM intake.submissions WHERE submission_id = $1 AND submitter_id = $2${lock ? " FOR UPDATE" : ""}`,
    [submissionId, me.submitterId]
  )
  if (!rows[0]) throw new ConnectError(`Submission ${JSON.stringify(submissionId)} is not one of yours.`, Code.NotFound)
  return rows[0]
}

/** The rules every save shares before its files are looked at. */
function checkSave(acceptedTerms: string, note: string): void {
  if (acceptedTerms !== TERMS_VERSION)
    throw new ConnectError(
      `The submission terms are now version ${TERMS_VERSION}, and the save accepted ${JSON.stringify(acceptedTerms)}. Reload the page, read the terms, and accept them.`,
      Code.FailedPrecondition
    )
  if (note.length > MAX_NOTE_CHARS) throw new ConnectError(`The note is ${note.length} characters; at most ${MAX_NOTE_CHARS}.`, Code.InvalidArgument)
}

function refuseProblems(checked: Checked): void {
  if (checked.problems.length === 0) return
  const lines = checked.problems.map((p) => (p.path ? `${p.path}: ${p.message}` : p.message))
  throw new ConnectError(`The submission was not saved:\n${lines.join("\n")}`, Code.InvalidArgument)
}

/** Refuses the save once the submitter has saved too much in the last 24 hours. */
async function checkQuota(db: Db, me: Submitter, newSubmission: boolean): Promise<void> {
  const { rows } = await db.query<{ revisions: number; created: number }>(
    `SELECT count(*)::int AS revisions, count(*) FILTER (WHERE revision = 1)::int AS created
       FROM intake.revisions WHERE created_by = $1 AND created_at > now() - interval '24 hours'`,
    [me.submitterId]
  )
  const { revisions, created } = rows[0]!
  if (revisions >= MAX_REVISIONS_PER_DAY || (newSubmission && created >= MAX_NEW_SUBMISSIONS_PER_DAY))
    throw new ConnectError(
      `You have saved ${revisions} revisions and ${created} new submissions in the last 24 hours, the most allowed (${MAX_REVISIONS_PER_DAY} and ${MAX_NEW_SUBMISSIONS_PER_DAY}). Try again tomorrow.`,
      Code.ResourceExhausted
    )
}

async function insertRevision(db: Db, me: Submitter, submissionId: string, n: number, files: readonly TextFile[], hash: string, note: string): Promise<RevisionRow> {
  const { rows } = await db.query<RevisionRow>(
    `INSERT INTO intake.revisions (submission_id, revision, files, content_sha256, note, terms_version, license, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING ${REVISION_COLUMNS}`,
    [submissionId, n, JSON.stringify(files), hash, note, TERMS_VERSION, LICENSE, me.submitterId]
  )
  return rows[0]!
}

/** The files of `base` with `changes` applied, or INVALID_ARGUMENT for a change that does not
 * apply. */
function applyChanges(base: readonly TextFile[], changes: readonly FileChange[]): InputFile[] {
  const files = new Map<string, Uint8Array>(base.map((f) => [f.path, encoder.encode(f.content)]))
  const touched = new Set<string>()
  for (const change of changes) {
    if (touched.has(change.path)) throw new ConnectError(`${change.path} is changed twice in one save.`, Code.InvalidArgument)
    touched.add(change.path)
    switch (change.change.case) {
      case "content":
        files.set(change.path, change.change.value)
        break
      case "delete":
        if (!change.change.value) throw new ConnectError(`${change.path}: \`delete\` must be true.`, Code.InvalidArgument)
        if (!files.delete(change.path)) throw new ConnectError(`${change.path} cannot be deleted: it is not among the files.`, Code.InvalidArgument)
        break
      default:
        throw new ConnectError(`${change.path}: a change needs new content or a delete.`, Code.InvalidArgument)
    }
  }
  return [...files].map(([path, content]) => ({ path, content }))
}

async function newestFiles(db: Db, submissionId: string, n: number): Promise<{ files: TextFile[]; hash: string }> {
  const { rows } = await db.query<{ files: TextFile[]; content_sha256: string }>(
    "SELECT files, content_sha256 FROM intake.revisions WHERE submission_id = $1 AND revision = $2",
    [submissionId, n]
  )
  return { files: rows[0]!.files, hash: rows[0]!.content_sha256 }
}

export const submissions: ServiceImpl<typeof SubmissionService> = {
  async createSubmission(req, context) {
    const { call, me } = await signedIn(context)
    await limitWrites(call)
    checkSave(req.acceptedTerms, req.note)
    const checked = checkFiles(req.files)
    refuseProblems(checked)
    await verifyTurnstile(call.env, req.turnstileToken, call.clientIp)
    const hash = await contentSha256(checked.files)
    return withDb(call.env, (db) =>
      tx(db, async () => {
        await checkQuota(db, me, true)
        const id = newSubmissionId()
        let row: SubmissionRow
        try {
          const { rows } = await db.query<SubmissionRow>(
            `INSERT INTO intake.submissions (submission_id, submitter_id, case_id, state, newest_revision, category, description)
             VALUES ($1, $2, $3, 'submitted', 1, $4, $5) RETURNING ${SUBMISSION_COLUMNS}`,
            [id, me.submitterId, checked.caseId, checked.category, checked.description]
          )
          row = rows[0]!
        } catch (err) {
          if (isUniqueViolation(err, "submissions_live_case_id"))
            throw new ConnectError(
              `Case id ${checked.caseId} is taken by another submission. Change \`id\` in case.yaml.`,
              Code.AlreadyExists,
              undefined,
              undefined,
              err
            )
          throw err
        }
        const rev = await insertRevision(db, me, id, 1, checked.files, hash, req.note)
        return { submission: submission(row), revision: revision(rev) }
      })
    )
  },

  async updateSubmission(req, context) {
    const { call, me } = await signedIn(context)
    await limitWrites(call)
    checkSave(req.acceptedTerms, req.note)
    // Read and check without a transaction: Turnstile is called before anything is locked.
    const { current, checked, hash, unchanged } = await withDb(call.env, async (db) => {
      const current = await ownSubmission(db, me, req.submissionId)
      if (current.state === "withdrawn") throw new ConnectError(`Submission ${current.submission_id} was withdrawn; it takes no more changes.`, Code.FailedPrecondition)
      if (req.baseRevision !== current.newest_revision)
        throw new ConnectError(
          `The changes were made against revision ${req.baseRevision}, but revision ${current.newest_revision} is the newest. Reload it and make the changes again.`,
          Code.Aborted
        )
      const base = await newestFiles(db, current.submission_id, current.newest_revision)
      const checked = checkFiles(applyChanges(base.files, req.changes))
      refuseProblems(checked)
      if (checked.caseId !== current.case_id)
        throw new ConnectError(
          `case.yaml must keep \`id: ${current.case_id}\`. To submit a different case, make a new submission.`,
          Code.InvalidArgument
        )
      const hash = await contentSha256(checked.files)
      return { current, checked, hash, unchanged: hash === base.hash }
    })
    if (unchanged) {
      const rev = await withDb(call.env, (db) => revisionRow(db, current.submission_id, current.newest_revision))
      return { submission: submission(current), revision: revision(rev), created: false }
    }
    await verifyTurnstile(call.env, req.turnstileToken, call.clientIp)
    return withDb(call.env, (db) =>
      tx(db, async () => {
        const locked = await ownSubmission(db, me, req.submissionId, true)
        if (locked.state === "withdrawn") throw new ConnectError(`Submission ${locked.submission_id} was withdrawn; it takes no more changes.`, Code.FailedPrecondition)
        if (locked.newest_revision !== req.baseRevision)
          throw new ConnectError(
            `Revision ${locked.newest_revision} was saved meanwhile, and the changes were made against ${req.baseRevision}. Reload it and make the changes again.`,
            Code.Aborted
          )
        await checkQuota(db, me, false)
        const n = locked.newest_revision + 1
        const rev = await insertRevision(db, me, locked.submission_id, n, checked.files, hash, req.note)
        // A new revision goes back to the maintainers; a revision accepted before stays accepted.
        const { rows } = await db.query<SubmissionRow>(
          `UPDATE intake.submissions
              SET newest_revision = $2, state = 'submitted', review_note = '', category = $3, description = $4, updated_at = now()
            WHERE submission_id = $1 RETURNING ${SUBMISSION_COLUMNS}`,
          [locked.submission_id, n, checked.category, checked.description]
        )
        return { submission: submission(rows[0]!), revision: revision(rev), created: true }
      })
    )
  },

  async getSubmission(req, context) {
    const { call, me } = await signedIn(context)
    return withDb(call.env, async (db) => {
      const row = await ownSubmission(db, me, req.submissionId)
      const { rows } = await db.query<RevisionRow>(
        `SELECT ${REVISION_COLUMNS} FROM intake.revisions WHERE submission_id = $1 ORDER BY revision DESC`,
        [row.submission_id]
      )
      return { submission: submission(row), revisions: rows.map(revision) }
    })
  },

  async getSubmissionRevision(req, context) {
    const { call, me } = await signedIn(context)
    return withDb(call.env, async (db) => {
      const row = await ownSubmission(db, me, req.submissionId)
      const n = req.revision === 0 ? row.newest_revision : req.revision
      const { rows } = await db.query<RevisionRow & { files: TextFile[] }>(
        `SELECT ${REVISION_COLUMNS}, files FROM intake.revisions WHERE submission_id = $1 AND revision = $2`,
        [row.submission_id, n]
      )
      if (!rows[0]) throw new ConnectError(`Submission ${row.submission_id} has no revision ${n}; its newest is ${row.newest_revision}.`, Code.NotFound)
      return { revision: revision(rows[0]), files: rows[0].files.map((f) => ({ path: f.path, content: encoder.encode(f.content) })) }
    })
  },

  async listMySubmissions(_req, context) {
    const { call, me } = await signedIn(context)
    const { rows } = await withDb(call.env, (db) =>
      db.query<SubmissionRow>(`SELECT ${SUBMISSION_COLUMNS} FROM intake.submissions WHERE submitter_id = $1 ORDER BY updated_at DESC`, [me.submitterId])
    )
    return { submissions: rows.map(submission) }
  },

  async withdrawSubmission(req, context) {
    const { call, me } = await signedIn(context)
    return withDb(call.env, async (db) => {
      const row = await ownSubmission(db, me, req.submissionId)
      if (row.state === "withdrawn") return { submission: submission(row) }
      const { rows } = await db.query<SubmissionRow>(
        `UPDATE intake.submissions SET state = 'withdrawn', updated_at = now() WHERE submission_id = $1 RETURNING ${SUBMISSION_COLUMNS}`,
        [row.submission_id]
      )
      return { submission: submission(rows[0]!) }
    })
  },

  async checkFiles(req, context) {
    const { call } = await signedIn(context)
    await limitWrites(call)
    return { problems: checkFiles(req.files).problems }
  },
}

async function revisionRow(db: Db, submissionId: string, n: number): Promise<RevisionRow> {
  const { rows } = await db.query<RevisionRow>(`SELECT ${REVISION_COLUMNS} FROM intake.revisions WHERE submission_id = $1 AND revision = $2`, [submissionId, n])
  return rows[0]!
}

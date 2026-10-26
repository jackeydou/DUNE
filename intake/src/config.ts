// What the site accepts. GetSiteConfig hands these to the pages; the rules behind them are in
// docs/services/intake.md#checks.

/** The version of the submission terms the pages show. Change it whenever the terms change: a
 * save naming an older version is refused, so nobody saves under terms they did not see. */
export const TERMS_VERSION = "2026-10-25"
export const LICENSE = "CC-BY-4.0"

/** The workspace every submitted case names. Accepted cases are pushed into it in the case
 * library. */
export const WORKSPACE = "community"

/** Images a sandbox profile may use, matched exactly, tag included. Grows only after the
 * maintainers have looked at an image. */
export const ALLOWED_IMAGES: readonly string[] = ["busybox:latest", "python:3.12-slim", "swarmeval/display:dev"]

export const MAX_FILES = 200
export const MAX_FILE_BYTES = 256 * 1024
export const MAX_TOTAL_BYTES = 1024 * 1024
export const MAX_NOTE_CHARS = 1000
/** `category` and `description` are kept for the list at most this long. */
export const MAX_SUMMARY_CHARS = 500

/** Per submitter, over the last 24 hours. */
export const MAX_NEW_SUBMISSIONS_PER_DAY = 10
export const MAX_REVISIONS_PER_DAY = 50

/** A session unused this long ends; and this long after sign-in, however active. edge's
 * defaults. */
export const SESSION_IDLE_MS = 24 * 60 * 60 * 1000
export const SESSION_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
/** last_seen_at is rewritten at most this often, so reads do not each become a write. */
export const SESSION_TOUCH_MS = 60 * 1000

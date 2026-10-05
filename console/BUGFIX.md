# Bug fixes

## 2026-10-04 — A changed binary file is missing from a revision diff

**Symptom.** Two revisions holding different binary files of the same length and mode at one
path showed no difference for that path.
**Root cause.** `diffRevisions` compared the files' descriptions, and a binary file is
described as `binary, N bytes` whatever its bytes are.
**Fix.** Equality is by content, link target, and mode (`sameFile`); the description is only
what the patch shows, with a line saying the binary content differs. `src/lib/files.ts`.
**Guard.** `src/lib/files.test.ts`: "binary files of one length and different bytes are a
change".
**Touches.** Text files were compared by their text, which is their content, so their diffs
are unchanged. Reported by Codex review on #24.

## 2026-10-04 — The Runs page's workspace filter misses older runs

**Symptom.** With 500 newer runs in other workspaces, choosing a workspace showed no runs, and
the workspace could be missing from the selector.
**Root cause.** The page asked for the newest 500 runs and filtered them by workspace in the
browser, and built the selector from those same runs.
**Fix.** `ListRuns` takes `workspace` (Control API, edge, and the public API), so the filter
applies before the limit; the selector lists the case library's workspaces. `src/pages/
Runs.tsx`.
**Guard.** `tests/control/test_case_library.py::test_runs_are_submitted_by_revision_and_name_it`
for the server's filter; `e2e/threshold.spec.ts` selects the workspace.
**Touches.** The other filters (case, suite, submission, status) were already the server's. The
page still shows at most 500 runs; there is no paging. Reported by Codex review on #24.

## 2026-10-04 — An expired session leaves the signed-in pages on screen

**Symptom.** After a browser session reached its idle or maximum age, the console kept showing
its pages while every call failed `UNAUTHENTICATED`; Sign out failed too, and only a manual
reload reached the sign-in form.
**Root cause.** Who is signed in was asked once and cached, and nothing looked at the code of
later failures.
**Fix.** Any query, mutation, or event stream that fails `UNAUTHENTICATED` ends the session in
the app (`endSessionOn`), which shows the sign-in form; such a failure is not retried. Sign out
reloads the page whether or not its call succeeds. `src/session.tsx`, `src/router.tsx`,
`src/lib/useRunEvents.ts`.
**Guard.** `e2e/threshold.spec.ts`: the session's cookie is cleared with a page open, and the
next call puts the sign-in form back.
**Touches.** A call made outside TanStack Query has to call `endSessionOn` itself, as the
event stream does. `PERMISSION_DENIED` (a member on an admin page) is not a lost session and
is left alone. Reported by Codex review on #24.

# Changelog

## [Unreleased]

### Added
- The intake site's Worker (`spec/2026-10-25-public-case-intake`, P1): GitHub sign-in with no
  scopes and `SameSite=Strict` sessions; `SiteService`, `IntakeAuthService`, and
  `SubmissionService` over Connect's JSON protocol, refusing calls from any other origin;
  submissions as append-only revisions with review states, withdrawal, a daily quota per
  submitter, and a write limit per client address; the site's checks on every save and in
  `CheckFiles` (text files and size limits, `workspace: community`, no case code, no
  `web_request`, listed images only, no live credentials); Turnstile on every save; the terms
  version and CC BY 4.0 recorded per revision. Neon Postgres through Hyperdrive, with
  `setup.sql` for the roles and goose migrations.

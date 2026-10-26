-- +goose Up
CREATE TABLE intake.submitters (
    submitter_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    github_id bigint NOT NULL UNIQUE,
    github_login text NOT NULL,
    name text NOT NULL DEFAULT '',
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE intake.sessions (
    session_hash bytea PRIMARY KEY,
    submitter_id bigint NOT NULL REFERENCES intake.submitters (submitter_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_submitter_id ON intake.sessions (submitter_id);

CREATE TABLE intake.submissions (
    submission_id text PRIMARY KEY,
    submitter_id bigint NOT NULL REFERENCES intake.submitters (submitter_id),
    case_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('submitted', 'triaged', 'accepted', 'rejected', 'withdrawn')),
    review_note text NOT NULL DEFAULT '',
    round text NOT NULL DEFAULT '',
    accepted_revision integer NOT NULL DEFAULT 0,
    newest_revision integer NOT NULL,
    category text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- A case id belongs to one live submission; withdrawing frees it.
CREATE UNIQUE INDEX submissions_live_case_id ON intake.submissions (case_id) WHERE state <> 'withdrawn';
CREATE INDEX submissions_submitter_id ON intake.submissions (submitter_id, updated_at DESC);

-- Never updated or deleted: a revision is what a submitter accepted the terms for.
CREATE TABLE intake.revisions (
    submission_id text NOT NULL REFERENCES intake.submissions (submission_id),
    revision integer NOT NULL CHECK (revision > 0),
    -- [{"path": ..., "content": ...}], sorted by path; content is the file's text.
    files jsonb NOT NULL,
    content_sha256 text NOT NULL,
    note text NOT NULL DEFAULT '',
    terms_version text NOT NULL,
    license text NOT NULL,
    created_by bigint NOT NULL REFERENCES intake.submitters (submitter_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (submission_id, revision)
);
CREATE INDEX revisions_created_by ON intake.revisions (created_by, created_at);

-- +goose Down
DROP TABLE intake.revisions;
DROP TABLE intake.submissions;
DROP TABLE intake.sessions;
DROP TABLE intake.submitters;

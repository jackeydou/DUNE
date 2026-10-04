-- +goose Up
CREATE TABLE tenant.users (
    user_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'member')),
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant.sessions (
    session_hash bytea PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES tenant.users (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_user_id ON tenant.sessions (user_id);

CREATE TABLE tenant.api_tokens (
    token_id text PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE,
    user_id bigint NOT NULL REFERENCES tenant.users (user_id),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    last_used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX api_tokens_user_id ON tenant.api_tokens (user_id);

-- +goose Down
DROP TABLE tenant.api_tokens;
DROP TABLE tenant.sessions;
DROP TABLE tenant.users;

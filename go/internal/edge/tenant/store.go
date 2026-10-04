// Package tenant owns edge's `tenant` schema: users, browser sessions, and API tokens
// (docs/services/edge.md). No other service reads it.
package tenant

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Role is what a user may do. Every user sees every workspace (v1 spec §9, question 8).
type Role string

const (
	RoleMember Role = "member"
	RoleAdmin  Role = "admin"
)

var (
	// ErrNotFound is an unknown user or token, or a session or token that is no longer valid.
	ErrNotFound = errors.New("not found")
	// ErrExists is a username that is taken.
	ErrExists = errors.New("already exists")
)

// usernamePattern matches the CHECK constraint on tenant.users.username.
var usernamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// CheckUsername reports why a new username is refused, or nil.
func CheckUsername(name string) error {
	if !usernamePattern.MatchString(name) {
		return fmt.Errorf("username %q is not valid: use 1 to 64 lowercase letters, digits, `.`, `_`, and `-`, starting with a letter or digit", name)
	}
	return nil
}

type User struct {
	ID           int64
	Username     string
	PasswordHash string
	Role         Role
	Disabled     bool
	CreatedAt    time.Time
}

type Token struct {
	ID         string
	Name       string
	CreatedAt  time.Time
	ExpiresAt  *time.Time
	LastUsedAt *time.Time
	RevokedAt  *time.Time
}

// touchEvery bounds how often a request rewrites a session's last_seen_at or a token's
// last_used_at, so reads do not turn into a write per request.
const touchEvery = time.Minute

type Store struct {
	pool *pgxpool.Pool
}

func NewStore(pool *pgxpool.Pool) *Store {
	return &Store{pool: pool}
}

const userColumns = "user_id, username, password_hash, role, disabled_at IS NOT NULL, created_at"

func scanUser(row pgx.Row) (User, error) {
	var u User
	err := row.Scan(&u.ID, &u.Username, &u.PasswordHash, &u.Role, &u.Disabled, &u.CreatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, ErrNotFound
	}
	return u, err
}

func (s *Store) CreateUser(ctx context.Context, username, passwordHash string, role Role) (User, error) {
	u, err := scanUser(s.pool.QueryRow(ctx,
		`INSERT INTO tenant.users (username, password_hash, role) VALUES ($1, $2, $3) RETURNING `+userColumns,
		username, passwordHash, role))
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return User{}, fmt.Errorf("user %q: %w", username, ErrExists)
	}
	return u, err
}

func (s *Store) UserByName(ctx context.Context, username string) (User, error) {
	return scanUser(s.pool.QueryRow(ctx, `SELECT `+userColumns+` FROM tenant.users WHERE username = $1`, username))
}

func (s *Store) ListUsers(ctx context.Context) ([]User, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+userColumns+` FROM tenant.users ORDER BY username`)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (User, error) { return scanUser(r) })
}

// SetDisabled disables or enables a user. Disabling also ends their sessions, in the same
// transaction; their tokens stay stored but are refused while the user is disabled.
func (s *Store) SetDisabled(ctx context.Context, username string, disabled bool) (User, error) {
	var u User
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		var err error
		u, err = scanUser(tx.QueryRow(ctx,
			`UPDATE tenant.users SET disabled_at = CASE WHEN $2 THEN coalesce(disabled_at, now()) END
			 WHERE username = $1 RETURNING `+userColumns, username, disabled))
		if err != nil || !disabled {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM tenant.sessions WHERE user_id = $1`, u.ID)
		return err
	})
	return u, err
}

// SetPassword stores a new hash and ends the user's sessions, except keep (the session that
// asked, or nil).
func (s *Store) SetPassword(ctx context.Context, userID int64, passwordHash string, keep []byte) error {
	return pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE tenant.users SET password_hash = $2 WHERE user_id = $1`, userID, passwordHash)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return fmt.Errorf("user %d: %w", userID, ErrNotFound)
		}
		_, err = tx.Exec(ctx, `DELETE FROM tenant.sessions WHERE user_id = $1 AND session_hash IS DISTINCT FROM $2`, userID, keep)
		return err
	})
}

// CreateSession stores a session that expires after maxAge however active it is. It also
// deletes sessions that have expired, so the table does not grow with abandoned ones.
func (s *Store) CreateSession(ctx context.Context, userID int64, hash []byte, maxAge, idle time.Duration) (time.Time, error) {
	var expires time.Time
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx,
			`DELETE FROM tenant.sessions WHERE expires_at <= now() OR last_seen_at <= now() - make_interval(secs => $1)`,
			idle.Seconds()); err != nil {
			return err
		}
		return tx.QueryRow(ctx,
			`INSERT INTO tenant.sessions (session_hash, user_id, expires_at)
			 VALUES ($1, $2, now() + make_interval(secs => $3)) RETURNING expires_at`,
			hash, userID, maxAge.Seconds()).Scan(&expires)
	})
	return expires, err
}

// SessionUser returns the user of a session that has not expired, has been used within idle,
// and belongs to an enabled user, and marks it used. Anything else is ErrNotFound.
func (s *Store) SessionUser(ctx context.Context, hash []byte, idle time.Duration) (User, error) {
	var u User
	var lastSeen time.Time
	err := s.pool.QueryRow(ctx,
		`SELECT u.user_id, u.username, u.password_hash, u.role, u.created_at, s.last_seen_at
		 FROM tenant.sessions s JOIN tenant.users u USING (user_id)
		 WHERE s.session_hash = $1 AND u.disabled_at IS NULL
		   AND s.expires_at > now() AND s.last_seen_at > now() - make_interval(secs => $2)`,
		hash, idle.Seconds()).Scan(&u.ID, &u.Username, &u.PasswordHash, &u.Role, &u.CreatedAt, &lastSeen)
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, ErrNotFound
	}
	if err != nil || time.Since(lastSeen) < touchEvery {
		return u, err
	}
	_, err = s.pool.Exec(ctx, `UPDATE tenant.sessions SET last_seen_at = now() WHERE session_hash = $1`, hash)
	return u, err
}

func (s *Store) DeleteSession(ctx context.Context, hash []byte) error {
	_, err := s.pool.Exec(ctx, `DELETE FROM tenant.sessions WHERE session_hash = $1`, hash)
	return err
}

const tokenColumns = "token_id, name, created_at, expires_at, last_used_at, revoked_at"

func scanToken(row pgx.Row) (Token, error) {
	var t Token
	err := row.Scan(&t.ID, &t.Name, &t.CreatedAt, &t.ExpiresAt, &t.LastUsedAt, &t.RevokedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return Token{}, ErrNotFound
	}
	return t, err
}

func (s *Store) CreateToken(ctx context.Context, userID int64, tokenID, name string, hash []byte, expires *time.Time) (Token, error) {
	return scanToken(s.pool.QueryRow(ctx,
		`INSERT INTO tenant.api_tokens (token_id, token_hash, user_id, name, expires_at)
		 VALUES ($1, $2, $3, $4, $5) RETURNING `+tokenColumns,
		tokenID, hash, userID, name, expires))
}

// TokenUser returns the user of a token that is neither revoked nor expired and belongs to an
// enabled user, and marks the token used. Anything else is ErrNotFound.
func (s *Store) TokenUser(ctx context.Context, hash []byte) (User, error) {
	var u User
	var lastUsed *time.Time
	err := s.pool.QueryRow(ctx,
		`SELECT u.user_id, u.username, u.password_hash, u.role, u.created_at, t.last_used_at
		 FROM tenant.api_tokens t JOIN tenant.users u USING (user_id)
		 WHERE t.token_hash = $1 AND u.disabled_at IS NULL
		   AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())`,
		hash).Scan(&u.ID, &u.Username, &u.PasswordHash, &u.Role, &u.CreatedAt, &lastUsed)
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, ErrNotFound
	}
	if err != nil || (lastUsed != nil && time.Since(*lastUsed) < touchEvery) {
		return u, err
	}
	_, err = s.pool.Exec(ctx, `UPDATE tenant.api_tokens SET last_used_at = now() WHERE token_hash = $1`, hash)
	return u, err
}

// ListTokens returns a user's tokens, newest first, revoked ones included.
func (s *Store) ListTokens(ctx context.Context, userID int64) ([]Token, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT `+tokenColumns+` FROM tenant.api_tokens WHERE user_id = $1 ORDER BY created_at DESC, token_id`, userID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (Token, error) { return scanToken(r) })
}

// RevokeToken revokes one of userID's tokens. Revoking it again keeps the first revocation time.
// Another user's token is ErrNotFound.
func (s *Store) RevokeToken(ctx context.Context, userID int64, tokenID string) (Token, error) {
	return scanToken(s.pool.QueryRow(ctx,
		`UPDATE tenant.api_tokens SET revoked_at = coalesce(revoked_at, now())
		 WHERE token_id = $1 AND user_id = $2 RETURNING `+tokenColumns, tokenID, userID))
}

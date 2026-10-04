package tenant

import (
	"context"
	"embed"
	"fmt"
	"io/fs"
	"log/slog"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed migrations/*.sql
var migrations embed.FS

// Migrate brings the `tenant` schema to the latest version. goose keeps its version table in
// that schema, apart from the Alembic version table of the other schemas, and holds a Postgres
// session lock so that edge replicas starting together migrate once.
func Migrate(ctx context.Context, pool *pgxpool.Pool, log *slog.Logger) error {
	if _, err := pool.Exec(ctx, "CREATE SCHEMA IF NOT EXISTS tenant"); err != nil {
		return fmt.Errorf("create schema tenant: %w", err)
	}
	dir, err := fs.Sub(migrations, "migrations")
	if err != nil {
		return err
	}
	locker, err := lock.NewPostgresSessionLocker()
	if err != nil {
		return err
	}
	db := stdlib.OpenDBFromPool(pool)
	defer func() { _ = db.Close() }() // closes only this handle; the pool stays open
	provider, err := goose.NewProvider(goose.DialectPostgres, db, dir,
		goose.WithTableName("tenant.goose_db_version"),
		goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true),
	)
	if err != nil {
		return fmt.Errorf("tenant migrations: %w", err)
	}
	results, err := provider.Up(ctx)
	if err != nil {
		return fmt.Errorf("migrate schema tenant: %w", err)
	}
	for _, r := range results {
		log.Info("tenant migration applied", "version", r.Source.Version, "took", r.Duration)
	}
	return nil
}

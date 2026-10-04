// Command edge is SwarmEval's public entry point: it serves the public API (swarmeval.api.v1)
// and manages users. Behavior: docs/services/edge.md.
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/jackeydou/DUNE/go/internal/edge"
	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
)

const databaseEnv = "SWARMEVAL_DATABASE_URL"

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	root := &cobra.Command{
		Use:           "edge",
		Short:         "SwarmEval's public entry point",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.AddCommand(serveCommand(log), userCommand(log))
	if err := root.Execute(); err != nil {
		log.Error("edge stopped", "err", err)
		os.Exit(1)
	}
}

// openTenant connects to the database named by SWARMEVAL_DATABASE_URL and migrates the tenant
// schema.
func openTenant(ctx context.Context, log *slog.Logger) (*pgxpool.Pool, error) {
	url := os.Getenv(databaseEnv)
	if url == "" {
		return nil, fmt.Errorf("%s is not set: give the Postgres URL, such as postgresql://swarmeval:…@db:5432/swarmeval", databaseEnv)
	}
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		return nil, fmt.Errorf("connect to Postgres from %s: %w", databaseEnv, err)
	}
	if err := tenant.Migrate(ctx, pool, log); err != nil {
		pool.Close()
		return nil, err
	}
	return pool, nil
}

func isLoopback(listen string) bool {
	host, _, err := net.SplitHostPort(listen)
	if err != nil {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func serveCommand(log *slog.Logger) *cobra.Command {
	var (
		listen, publicURL, certFile, keyFile, control string
		idle, maxAge                                  time.Duration
	)
	cmd := &cobra.Command{
		Use:   "serve",
		Short: "Serve the public API",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			public, err := edge.ParsePublicURL(publicURL)
			if err != nil {
				return fmt.Errorf("--public-url: %w", err)
			}
			if (certFile == "") != (keyFile == "") {
				return errors.New("--tls-cert and --tls-key go together: give both to serve https, or neither")
			}
			useTLS := certFile != ""
			if !useTLS && !isLoopback(listen) {
				return fmt.Errorf("--listen %s is not a loopback address, and edge has no certificate: give --tls-cert and --tls-key, or listen on 127.0.0.1 behind a TLS-terminating proxy", listen)
			}
			if control == "" {
				return errors.New("--control is required: the orchestrator's Control API address, such as control:7090")
			}
			if idle <= 0 || maxAge < idle {
				return fmt.Errorf("--session-idle %s and --session-max-age %s: both must be positive, and the max age at least the idle limit", idle, maxAge)
			}

			ctx, stop := signal.NotifyContext(cmd.Context(), syscall.SIGINT, syscall.SIGTERM)
			defer stop()
			pool, err := openTenant(ctx, log)
			if err != nil {
				return err
			}
			defer pool.Close()

			cfg := edge.Config{PublicURL: public, SessionIdle: idle, SessionMaxAge: maxAge}
			handler := edge.NewHandler(cfg, tenant.NewStore(pool), edge.NewControlClient("http://"+control), log)
			var protocols http.Protocols
			protocols.SetHTTP1(true)
			if useTLS {
				protocols.SetHTTP2(true)
			} else {
				// The CLI speaks gRPC, which needs HTTP/2, also on a loopback listener.
				protocols.SetUnencryptedHTTP2(true)
			}
			server := &http.Server{
				Addr:              listen,
				Handler:           handler,
				Protocols:         &protocols,
				ReadHeaderTimeout: 10 * time.Second,
				TLSConfig:         &tls.Config{MinVersion: tls.VersionTLS12},
				ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
			}
			go func() {
				<-ctx.Done()
				shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				_ = server.Shutdown(shutdown) // streams still open after the timeout are cut
			}()
			log.Info("edge serving", "listen", listen, "public_url", public.String(), "tls", useTLS, "control", control)
			if useTLS {
				err = server.ListenAndServeTLS(certFile, keyFile)
			} else {
				err = server.ListenAndServe()
			}
			if errors.Is(err, http.ErrServerClosed) {
				return nil
			}
			return err
		},
	}
	f := cmd.Flags()
	f.StringVar(&listen, "listen", "127.0.0.1:7443", "address to serve on. Without a certificate it must be a loopback address")
	f.StringVar(&publicURL, "public-url", "", "the address browsers use to reach edge, such as https://swarm.example.com (required)")
	f.StringVar(&certFile, "tls-cert", "", "PEM certificate chain for https")
	f.StringVar(&keyFile, "tls-key", "", "PEM private key for --tls-cert")
	f.StringVar(&control, "control", "", "the orchestrator's Control API, host:port (required)")
	f.DurationVar(&idle, "session-idle", 24*time.Hour, "end a browser session unused for this long")
	f.DurationVar(&maxAge, "session-max-age", 7*24*time.Hour, "end a browser session this long after sign-in")
	return cmd
}

func userCommand(log *slog.Logger) *cobra.Command {
	user := &cobra.Command{Use: "user", Short: "Manage users directly in the database"}
	var admin bool
	create := &cobra.Command{
		Use:   "create USERNAME",
		Short: "Create a user, such as the first admin. The password is read from standard input",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			username := args[0]
			if err := tenant.CheckUsername(username); err != nil {
				return err
			}
			password, err := readPassword(cmd)
			if err != nil {
				return err
			}
			if err := tenant.CheckPassword(password); err != nil {
				return err
			}
			pool, err := openTenant(cmd.Context(), log)
			if err != nil {
				return err
			}
			defer pool.Close()
			role := tenant.RoleMember
			if admin {
				role = tenant.RoleAdmin
			}
			created, err := tenant.NewStore(pool).CreateUser(cmd.Context(), username, tenant.HashPassword(password), role)
			if err != nil {
				return fmt.Errorf("create user %q: %w", username, err)
			}
			_, err = fmt.Fprintf(cmd.OutOrStdout(), "created %s %q\n", created.Role, created.Username)
			return err
		},
	}
	create.Flags().BoolVar(&admin, "admin", false, "give the user the admin role")
	user.AddCommand(create)
	return user
}

// readPassword prompts twice without echo on a terminal, and otherwise reads one line.
func readPassword(cmd *cobra.Command) (string, error) {
	fd := int(os.Stdin.Fd())
	if !term.IsTerminal(fd) {
		line, err := bufio.NewReader(cmd.InOrStdin()).ReadString('\n')
		if err != nil && line == "" {
			return "", fmt.Errorf("read the password from standard input: %w", err)
		}
		return strings.TrimRight(line, "\r\n"), nil
	}
	ask := func(prompt string) (string, error) {
		_, _ = fmt.Fprint(cmd.ErrOrStderr(), prompt)
		b, err := term.ReadPassword(fd)
		_, _ = fmt.Fprintln(cmd.ErrOrStderr())
		return string(b), err
	}
	first, err := ask("Password: ")
	if err != nil {
		return "", err
	}
	second, err := ask("Again: ")
	if err != nil {
		return "", err
	}
	if first != second {
		return "", errors.New("the passwords differ; run the command again")
	}
	return first, nil
}

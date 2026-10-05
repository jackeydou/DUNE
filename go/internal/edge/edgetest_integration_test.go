//go:build integration

package edge

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"connectrpc.com/connect"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go/modules/postgres"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
	"github.com/jackeydou/DUNE/go/internal/mtls"
)

const testBodyTimeout = 500 * time.Millisecond

// postgresImage matches tests/containers.py, so one pulled image serves both test suites.
const postgresImage = "postgres:18-alpine"

// adminURL reaches the container's maintenance database; each test creates its own database.
var adminURL string

func TestMain(m *testing.M) {
	ctx := context.Background()
	ctr, err := postgres.Run(ctx, postgresImage,
		postgres.WithDatabase("edge"), postgres.WithUsername("edge"), postgres.WithPassword("edge"),
		postgres.BasicWaitStrategies())
	if err != nil {
		fmt.Fprintln(os.Stderr, "start Postgres:", err)
		os.Exit(1)
	}
	adminURL, err = ctr.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Postgres URL:", err)
		os.Exit(1)
	}
	code := m.Run()
	_ = ctr.Terminate(ctx) // the process exits next; a leftover container is reaped by testcontainers
	os.Exit(code)
}

var databases atomic.Int64

// freshPool returns a pool on a new, migrated database of its own.
func freshPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	ctx := t.Context()
	name := fmt.Sprintf("t%d_%d", os.Getpid(), databases.Add(1))
	admin, err := pgxpool.New(ctx, adminURL)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	if _, err := admin.Exec(ctx, "CREATE DATABASE "+name); err != nil {
		t.Fatal(err)
	}
	pool, err := pgxpool.New(ctx, strings.Replace(adminURL, "/edge?", "/"+name+"?", 1))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := tenant.Migrate(ctx, pool, quietLog()); err != nil {
		t.Fatal(err)
	}
	return pool
}

func quietLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// h2cServer serves handler over HTTP/1.1 and unencrypted HTTP/2, as edge and the Control API do
// on loopback.
func h2cServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	srv := httptest.NewUnstartedServer(handler)
	srv.Config.Protocols = new(http.Protocols)
	srv.Config.Protocols.SetHTTP1(true)
	srv.Config.Protocols.SetUnencryptedHTTP2(true)
	srv.Start()
	t.Cleanup(srv.Close)
	return srv
}

// stack is edge in front of a fake Control API, on a database of its own.
type stack struct {
	url     string
	cfg     Config
	store   *tenant.Store
	pool    *pgxpool.Pool
	control *fakeControl
	auth    apiv1connect.AuthServiceClient
	users   apiv1connect.UserServiceClient
	runs    apiv1connect.RunServiceClient
	cases   apiv1connect.CaseServiceClient
	// analysisFake answers for the analysis service; analysis is edge's public client for it.
	analysisFake *fakeAnalysis
	analysis     apiv1connect.AnalysisServiceClient
}

func newStack(t *testing.T) *stack {
	t.Helper()
	pool := freshPool(t)
	control := newFakeControl()
	_, controlHandler := controlv1connect.NewControlServiceHandler(control)
	controlSrv := h2cServer(t, controlHandler)
	analysisFake := &fakeAnalysis{}
	_, analysisHandler := analysisv1connect.NewAnalysisServiceHandler(analysisFake)
	analysisSrv := h2cServer(t, analysisHandler)

	edgeSrv := httptest.NewUnstartedServer(nil)
	edgeSrv.Config.Protocols = new(http.Protocols)
	edgeSrv.Config.Protocols.SetHTTP1(true)
	edgeSrv.Config.Protocols.SetUnencryptedHTTP2(true)
	public, err := ParsePublicURL("http://" + edgeSrv.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	store := tenant.NewStore(pool)
	cfg := Config{
		PublicURL: public, SessionIdle: time.Hour, SessionMaxAge: 24 * time.Hour,
		// Short, so a test can outlast them; local requests arrive in well under that.
		BodyTimeout: testBodyTimeout, UploadTimeout: 5 * time.Second,
	}
	controlClient, err := NewControlClient(controlSrv.Listener.Addr().String(), mtls.Files{})
	if err != nil {
		t.Fatal(err)
	}
	analysisClient, err := NewAnalysisClient(analysisSrv.Listener.Addr().String(), mtls.Files{})
	if err != nil {
		t.Fatal(err)
	}
	edgeSrv.Config.Handler = NewHandler(cfg, store, controlClient, analysisClient, quietLog())
	edgeSrv.Start()
	t.Cleanup(edgeSrv.Close)

	client := edgeSrv.Client()
	return &stack{
		url:          edgeSrv.URL,
		cfg:          cfg,
		store:        store,
		pool:         pool,
		control:      control,
		auth:         apiv1connect.NewAuthServiceClient(client, edgeSrv.URL),
		users:        apiv1connect.NewUserServiceClient(client, edgeSrv.URL),
		runs:         apiv1connect.NewRunServiceClient(client, edgeSrv.URL),
		cases:        apiv1connect.NewCaseServiceClient(client, edgeSrv.URL),
		analysisFake: analysisFake,
		analysis:     apiv1connect.NewAnalysisServiceClient(client, edgeSrv.URL),
	}
}

// user creates a user straight in the store, as `edge user create` does.
func (s *stack) user(t *testing.T, name, password string, role tenant.Role) tenant.User {
	t.Helper()
	u, err := s.store.CreateUser(t.Context(), name, tenant.HashPassword(password), role)
	if err != nil {
		t.Fatal(err)
	}
	return u
}

// browser holds a session cookie and sends the Origin a page served by edge would.
type browser struct {
	origin string
	cookie string
}

func (b browser) sign(h http.Header) {
	h.Set("Origin", b.origin)
	if b.cookie != "" {
		h.Set("Cookie", b.cookie)
	}
}

// login signs in through AuthService.Login and returns a browser holding the session cookie.
func (s *stack) login(t *testing.T, name, password string) browser {
	t.Helper()
	b := browser{origin: s.url}
	req := connect.NewRequest(&apiv1.LoginRequest{Username: name, Password: password})
	b.sign(req.Header())
	res, err := s.auth.Login(t.Context(), req)
	if err != nil {
		t.Fatalf("sign in as %s: %v", name, err)
	}
	cookies, err := http.ParseSetCookie(res.Header().Get("Set-Cookie"))
	if err != nil {
		t.Fatal(err)
	}
	b.cookie = cookies.Name + "=" + cookies.Value
	return b
}

// token signs in through LoginForToken and returns the API token.
func (s *stack) token(t *testing.T, name, password string) string {
	t.Helper()
	res, err := s.auth.LoginForToken(t.Context(), connect.NewRequest(&apiv1.LoginForTokenRequest{
		Username: name, Password: password, TokenName: "laptop",
	}))
	if err != nil {
		t.Fatalf("token for %s: %v", name, err)
	}
	return res.Msg.GetToken()
}

func bearer[T any](token string, msg *T) *connect.Request[T] {
	req := connect.NewRequest(msg)
	req.Header().Set("Authorization", "Bearer "+token)
	return req
}

func asBrowser[T any](b browser, msg *T) *connect.Request[T] {
	req := connect.NewRequest(msg)
	b.sign(req.Header())
	return req
}

func wantCode(t *testing.T, err error, code connect.Code) {
	t.Helper()
	if connect.CodeOf(err) != code {
		t.Fatalf("got %v, want %s", err, code)
	}
}

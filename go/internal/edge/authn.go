package edge

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
)

// principal is the authenticated caller of a request.
type principal struct {
	user tenant.User
	// session is the hash of the session the request came with; nil for an API token.
	session []byte
}

type principalKey struct{}

func callerOf(ctx context.Context) principal {
	p, ok := ctx.Value(principalKey{}).(principal)
	if !ok {
		// Every procedure but the public ones runs behind the authenticator.
		panic("edge: handler reached without an authenticated caller")
	}
	return p
}

// publicProcedures need no credentials.
var publicProcedures = map[string]bool{
	apiv1connect.AuthServiceLoginProcedure:         true,
	apiv1connect.AuthServiceLoginForTokenProcedure: true,
}

// authenticator is a connect interceptor that resolves the caller of every request, from an
// API token (`Authorization: Bearer swm_…`) or else the session cookie, and refuses requests
// with neither.
//
// CSRF: a request whose Origin is not edge's public origin is refused, and a cookie-authenticated
// request must carry Origin at all. Browsers send Origin on every POST; Connect's JSON requests
// need `Content-Type: application/json`, which a cross-site HTML form cannot send.
type authenticator struct {
	store *tenant.Store
	cfg   Config
	log   *slog.Logger
}

func (a *authenticator) WrapUnary(next connect.UnaryFunc) connect.UnaryFunc {
	return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
		ctx, err := a.authenticate(ctx, req.Spec().Procedure, req.Header())
		if err != nil {
			return nil, err
		}
		return next(ctx, req)
	}
}

func (a *authenticator) WrapStreamingClient(next connect.StreamingClientFunc) connect.StreamingClientFunc {
	return next
}

func (a *authenticator) WrapStreamingHandler(next connect.StreamingHandlerFunc) connect.StreamingHandlerFunc {
	return func(ctx context.Context, conn connect.StreamingHandlerConn) error {
		ctx, err := a.authenticate(ctx, conn.Spec().Procedure, conn.RequestHeader())
		if err != nil {
			return err
		}
		return next(ctx, conn)
	}
}

func (a *authenticator) authenticate(ctx context.Context, procedure string, header http.Header) (context.Context, error) {
	origin := header.Get("Origin")
	if origin != "" && origin != a.cfg.origin() {
		return nil, connect.NewError(connect.CodePermissionDenied, fmt.Errorf(
			"request from origin %q refused: edge accepts browser requests only from its public URL %s", origin, a.cfg.origin()))
	}
	if publicProcedures[procedure] {
		return ctx, nil
	}
	if authz := header.Get("Authorization"); authz != "" {
		token, ok := strings.CutPrefix(authz, "Bearer ")
		if !ok || !strings.HasPrefix(token, tenant.TokenPrefix) {
			return nil, connect.NewError(connect.CodeUnauthenticated, errors.New(
				"the Authorization header must be `Bearer swm_…`, an API token; create one in the console or with `swarm login`"))
		}
		user, err := a.store.TokenUser(ctx, tenant.SecretHash(token))
		if errors.Is(err, tenant.ErrNotFound) {
			return nil, connect.NewError(connect.CodeUnauthenticated, errors.New(
				"the API token is unknown, revoked, or expired, or its user is disabled; create a new one with `swarm login`"))
		}
		if err != nil {
			return nil, internalError(a.log, "look up API token", err)
		}
		return context.WithValue(ctx, principalKey{}, principal{user: user}), nil
	}
	cookie, err := (&http.Request{Header: header}).Cookie(a.cfg.cookieName())
	if err != nil || cookie.Value == "" {
		return nil, connect.NewError(connect.CodeUnauthenticated, errors.New(
			"sign in first: the request carries neither a session cookie nor an API token"))
	}
	if origin == "" {
		return nil, connect.NewError(connect.CodePermissionDenied, errors.New(
			"a request signed in by session cookie must carry an Origin header; the console's requests do, and the CLI uses an API token instead"))
	}
	hash := tenant.SecretHash(cookie.Value)
	user, err := a.store.SessionUser(ctx, hash, a.cfg.SessionIdle)
	if errors.Is(err, tenant.ErrNotFound) {
		return nil, connect.NewError(connect.CodeUnauthenticated, errors.New(
			"the session has expired or was ended; sign in again"))
	}
	if err != nil {
		return nil, internalError(a.log, "look up session", err)
	}
	return context.WithValue(ctx, principalKey{}, principal{user: user, session: hash}), nil
}

// internalError logs a failure the caller cannot act on, with its cause, and returns a
// CodeInternal error that names the step but not the cause.
func internalError(log *slog.Logger, step string, err error) error {
	if errors.Is(err, context.Canceled) {
		return connect.NewError(connect.CodeCanceled, err)
	}
	log.Error("edge request failed", "step", step, "err", err)
	return connect.NewError(connect.CodeInternal, fmt.Errorf("edge could not %s; the operator's log has the cause", step))
}

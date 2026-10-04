package edge

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"time"
	"unicode/utf8"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// hashSlots bounds concurrent argon2id computations: each takes 64 MiB and tens of
// milliseconds, so a burst of sign-ins must queue rather than exhaust memory.
const hashSlots = 4

type AuthService struct {
	store   *tenant.Store
	cfg     Config
	log     *slog.Logger
	limiter *loginLimiter
	hashing chan struct{}
	// dummyHash is verified against when the username is unknown, so an unknown user takes as
	// long to refuse as a wrong password.
	dummyHash string
}

func newAuthService(store *tenant.Store, cfg Config, log *slog.Logger) *AuthService {
	return &AuthService{
		store:     store,
		cfg:       cfg,
		log:       log,
		limiter:   newLoginLimiter(),
		hashing:   make(chan struct{}, hashSlots),
		dummyHash: tenant.HashPassword(tenant.NewSessionSecret()),
	}
}

// slot waits for a free hashing slot; release it with the returned func.
func (s *AuthService) slot(ctx context.Context) (func(), error) {
	select {
	case s.hashing <- struct{}{}:
		return func() { <-s.hashing }, nil
	case <-ctx.Done():
		return nil, connect.NewError(connect.CodeCanceled, ctx.Err())
	}
}

func (s *AuthService) verify(ctx context.Context, password, hash string) error {
	release, err := s.slot(ctx)
	if err != nil {
		return err
	}
	defer release()
	return tenant.VerifyPassword(password, hash)
}

func (s *AuthService) hash(ctx context.Context, password string) (string, error) {
	release, err := s.slot(ctx)
	if err != nil {
		return "", err
	}
	defer release()
	return tenant.HashPassword(password), nil
}

// errSignIn does not say which check failed, so a caller cannot learn which usernames exist.
func errSignIn() error {
	return connect.NewError(connect.CodeUnauthenticated, errors.New(
		"sign-in refused: the username or password is wrong, or the user is disabled"))
}

func locked(wait time.Duration) error {
	return connect.NewError(connect.CodeResourceExhausted, fmt.Errorf(
		"too many failed sign-ins for this username or from this address; try again in %s", wait.Round(time.Second)))
}

// signIn checks a username and password, throttled per username and per client address.
func (s *AuthService) signIn(ctx context.Context, username, password, peerAddr string) (tenant.User, error) {
	host, _, err := net.SplitHostPort(peerAddr)
	if err != nil {
		host = peerAddr
	}
	addrKey := "addr:" + host
	if tenant.CheckUsername(username) != nil {
		// No user has such a name. It is not kept as a throttle key, which could be megabytes
		// long, and costs no hash; the failure still counts against the address.
		if wait := s.limiter.wait(addrKey); wait > 0 {
			return tenant.User{}, locked(wait)
		}
		s.limiter.fail(addrKey)
		return tenant.User{}, errSignIn()
	}
	userKey := "user:" + username
	if wait := s.limiter.wait(userKey, addrKey); wait > 0 {
		return tenant.User{}, locked(wait)
	}
	user, err := s.store.UserByName(ctx, username)
	known := err == nil
	if err != nil && !errors.Is(err, tenant.ErrNotFound) {
		return tenant.User{}, internalError(s.log, "look up user", err)
	}
	hash := s.dummyHash
	if known {
		hash = user.PasswordHash
	}
	match := false
	if utf8.RuneCountInString(password) <= tenant.MaxPasswordLen {
		switch err := s.verify(ctx, password, hash); {
		case err == nil:
			match = true
		case errors.Is(err, tenant.ErrMismatch):
		default:
			return tenant.User{}, internalError(s.log, "check password", fmt.Errorf("user %q: %w", username, err))
		}
	}
	if !known || user.Disabled || !match {
		s.limiter.fail(userKey, addrKey)
		return tenant.User{}, errSignIn()
	}
	s.limiter.succeed(userKey)
	return user, nil
}

func (s *AuthService) Login(ctx context.Context, req *connect.Request[apiv1.LoginRequest]) (*connect.Response[apiv1.LoginResponse], error) {
	user, err := s.signIn(ctx, req.Msg.GetUsername(), req.Msg.GetPassword(), req.Peer().Addr)
	if err != nil {
		return nil, err
	}
	secret := tenant.NewSessionSecret()
	expires, err := s.store.CreateSession(ctx, user.ID, tenant.SecretHash(secret), s.cfg.SessionMaxAge, s.cfg.SessionIdle)
	if err != nil {
		return nil, internalError(s.log, "create session", err)
	}
	res := connect.NewResponse(&apiv1.LoginResponse{User: userProto(user), ExpiresAt: timestamppb.New(expires)})
	res.Header().Add("Set-Cookie", s.cfg.sessionCookie(secret, expires).String())
	return res, nil
}

func (s *AuthService) LoginForToken(ctx context.Context, req *connect.Request[apiv1.LoginForTokenRequest]) (*connect.Response[apiv1.LoginForTokenResponse], error) {
	expires, err := checkToken(req.Msg.GetTokenName(), req.Msg.GetExpiresAt())
	if err != nil {
		return nil, err
	}
	user, err := s.signIn(ctx, req.Msg.GetUsername(), req.Msg.GetPassword(), req.Peer().Addr)
	if err != nil {
		return nil, err
	}
	token, info, err := s.createToken(ctx, user, req.Msg.GetTokenName(), expires)
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&apiv1.LoginForTokenResponse{Token: token, Info: info}), nil
}

func (s *AuthService) Logout(ctx context.Context, _ *connect.Request[apiv1.LogoutRequest]) (*connect.Response[apiv1.LogoutResponse], error) {
	caller := callerOf(ctx)
	if caller.session == nil {
		return nil, connect.NewError(connect.CodeFailedPrecondition, errors.New(
			"this request used an API token, not a session; revoke the token instead"))
	}
	if err := s.store.DeleteSession(ctx, caller.session); err != nil {
		return nil, internalError(s.log, "end session", err)
	}
	res := connect.NewResponse(&apiv1.LogoutResponse{})
	res.Header().Add("Set-Cookie", s.cfg.clearedCookie().String())
	return res, nil
}

func (s *AuthService) WhoAmI(ctx context.Context, _ *connect.Request[apiv1.WhoAmIRequest]) (*connect.Response[apiv1.WhoAmIResponse], error) {
	return connect.NewResponse(&apiv1.WhoAmIResponse{User: userProto(callerOf(ctx).user)}), nil
}

func (s *AuthService) ChangePassword(ctx context.Context, req *connect.Request[apiv1.ChangePasswordRequest]) (*connect.Response[apiv1.ChangePasswordResponse], error) {
	caller := callerOf(ctx)
	if err := tenant.CheckPassword(req.Msg.GetNewPassword()); err != nil {
		return nil, connect.NewError(connect.CodeInvalidArgument, err)
	}
	// Checked like a sign-in, so a stolen session cannot guess the password freely.
	if _, err := s.signIn(ctx, caller.user.Username, req.Msg.GetCurrentPassword(), req.Peer().Addr); err != nil {
		return nil, err
	}
	hash, err := s.hash(ctx, req.Msg.GetNewPassword())
	if err != nil {
		return nil, err
	}
	if err := s.store.SetPassword(ctx, caller.user.ID, hash, caller.session); err != nil {
		return nil, internalError(s.log, "store password", err)
	}
	return connect.NewResponse(&apiv1.ChangePasswordResponse{}), nil
}

// checkToken validates a new token's name and expiry; it returns the expiry, nil for none.
func checkToken(name string, expiresAt *timestamppb.Timestamp) (*time.Time, error) {
	if n := utf8.RuneCountInString(name); n < 1 || n > 64 {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"token name %q must be 1 to 64 characters, such as the machine the token is for", name))
	}
	if expiresAt == nil {
		return nil, nil
	}
	expires := expiresAt.AsTime()
	if !expires.After(time.Now()) {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"token expiry %s is not in the future; leave it unset for a token that does not expire", expires.Format(time.RFC3339)))
	}
	return &expires, nil
}

func (s *AuthService) createToken(ctx context.Context, user tenant.User, name string, expires *time.Time) (string, *apiv1.ApiToken, error) {
	token, id := tenant.NewToken()
	stored, err := s.store.CreateToken(ctx, user.ID, id, name, tenant.SecretHash(token), expires)
	if err != nil {
		return "", nil, internalError(s.log, "store API token", err)
	}
	return token, tokenProto(stored), nil
}

func (s *AuthService) CreateToken(ctx context.Context, req *connect.Request[apiv1.CreateTokenRequest]) (*connect.Response[apiv1.CreateTokenResponse], error) {
	expires, err := checkToken(req.Msg.GetName(), req.Msg.GetExpiresAt())
	if err != nil {
		return nil, err
	}
	token, info, err := s.createToken(ctx, callerOf(ctx).user, req.Msg.GetName(), expires)
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&apiv1.CreateTokenResponse{Token: token, Info: info}), nil
}

func (s *AuthService) ListTokens(ctx context.Context, _ *connect.Request[apiv1.ListTokensRequest]) (*connect.Response[apiv1.ListTokensResponse], error) {
	tokens, err := s.store.ListTokens(ctx, callerOf(ctx).user.ID)
	if err != nil {
		return nil, internalError(s.log, "list API tokens", err)
	}
	res := &apiv1.ListTokensResponse{Tokens: make([]*apiv1.ApiToken, len(tokens))}
	for i, t := range tokens {
		res.Tokens[i] = tokenProto(t)
	}
	return connect.NewResponse(res), nil
}

func (s *AuthService) RevokeToken(ctx context.Context, req *connect.Request[apiv1.RevokeTokenRequest]) (*connect.Response[apiv1.RevokeTokenResponse], error) {
	token, err := s.store.RevokeToken(ctx, callerOf(ctx).user.ID, req.Msg.GetTokenId())
	if errors.Is(err, tenant.ErrNotFound) {
		return nil, connect.NewError(connect.CodeNotFound, fmt.Errorf(
			"you have no API token %q; ListTokens shows yours", req.Msg.GetTokenId()))
	}
	if err != nil {
		return nil, internalError(s.log, "revoke API token", err)
	}
	return connect.NewResponse(&apiv1.RevokeTokenResponse{Token: tokenProto(token)}), nil
}

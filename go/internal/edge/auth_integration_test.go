//go:build integration

package edge

import (
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

const pw = "correct horse battery staple"

func TestASessionSignsInAndOut(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	b := s.login(t, "ada", pw)

	me, err := s.auth.WhoAmI(t.Context(), asBrowser(b, &apiv1.WhoAmIRequest{}))
	if err != nil {
		t.Fatal(err)
	}
	if me.Msg.GetUser().GetUsername() != "ada" || me.Msg.GetUser().GetRole() != apiv1.Role_ROLE_MEMBER {
		t.Fatalf("WhoAmI: %v", me.Msg.GetUser())
	}

	res, err := s.auth.Logout(t.Context(), asBrowser(b, &apiv1.LogoutRequest{}))
	if err != nil {
		t.Fatal(err)
	}
	if c := res.Header().Get("Set-Cookie"); c == "" {
		t.Fatal("Logout did not clear the cookie")
	}
	_, err = s.auth.WhoAmI(t.Context(), asBrowser(b, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
}

func TestACookieNeedsTheRightOrigin(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	b := s.login(t, "ada", pw)

	noOrigin := connect.NewRequest(&apiv1.WhoAmIRequest{})
	noOrigin.Header().Set("Cookie", b.cookie)
	_, err := s.auth.WhoAmI(t.Context(), noOrigin)
	wantCode(t, err, connect.CodePermissionDenied)

	_, err = s.auth.WhoAmI(t.Context(), asBrowser(browser{origin: "https://evil.example", cookie: b.cookie}, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodePermissionDenied)

	// A sign-in posted from another site is refused too, so it cannot plant a session.
	login := connect.NewRequest(&apiv1.LoginRequest{Username: "ada", Password: pw})
	login.Header().Set("Origin", "https://evil.example")
	_, err = s.auth.Login(t.Context(), login)
	wantCode(t, err, connect.CodePermissionDenied)
}

func TestNoCredentialsIsRefused(t *testing.T) {
	s := newStack(t)
	_, err := s.runs.ListRuns(t.Context(), connect.NewRequest(&apiv1.ListRunsRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	_, err = s.auth.WhoAmI(t.Context(), bearer("not-a-token", &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	_, err = s.auth.WhoAmI(t.Context(), bearer("swm_unknown", &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	if len(s.control.requests) != 0 {
		t.Fatalf("the Control API was called: %v", s.control.requests)
	}
}

func TestUnknownUsersAndWrongPasswordsLookAlikeAndLockOut(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	login := func(user, password string) error {
		_, err := s.auth.Login(t.Context(), connect.NewRequest(&apiv1.LoginRequest{Username: user, Password: password}))
		return err
	}
	unknown, wrong := login("nobody", pw), login("ada", "wrong password!!")
	wantCode(t, unknown, connect.CodeUnauthenticated)
	wantCode(t, wrong, connect.CodeUnauthenticated)
	if unknown.Error() != wrong.Error() {
		t.Fatalf("the two refusals differ: %q vs %q", unknown, wrong)
	}
	for range freeFailures - 1 {
		_ = login("ada", "wrong password!!")
	}
	// Locked now, even with the right password.
	wantCode(t, login("ada", pw), connect.CodeResourceExhausted)
}

func TestDisabledUsersCannotSignIn(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	if _, err := s.store.SetDisabled(t.Context(), "ada", true); err != nil {
		t.Fatal(err)
	}
	_, err := s.auth.Login(t.Context(), connect.NewRequest(&apiv1.LoginRequest{Username: "ada", Password: pw}))
	wantCode(t, err, connect.CodeUnauthenticated)
}

func TestTokensWorkUntilRevokedOrExpired(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	if _, err := s.auth.WhoAmI(t.Context(), bearer(token, &apiv1.WhoAmIRequest{})); err != nil {
		t.Fatal(err)
	}
	second, err := s.auth.CreateToken(t.Context(), bearer(token, &apiv1.CreateTokenRequest{
		Name: "ci", ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}))
	if err != nil {
		t.Fatal(err)
	}
	listed, err := s.auth.ListTokens(t.Context(), bearer(token, &apiv1.ListTokensRequest{}))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(listed.Msg.GetTokens()); n != 2 || listed.Msg.GetTokens()[0].GetName() != "ci" {
		t.Fatalf("ListTokens: %v", listed.Msg.GetTokens())
	}
	if listed.Msg.GetTokens()[1].GetLastUsedAt() == nil {
		t.Fatal("a used token has no last_used_at")
	}

	if _, err := s.auth.RevokeToken(t.Context(), bearer(token, &apiv1.RevokeTokenRequest{TokenId: second.Msg.GetInfo().GetTokenId()})); err != nil {
		t.Fatal(err)
	}
	_, err = s.auth.WhoAmI(t.Context(), bearer(second.Msg.GetToken(), &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)

	if _, err := s.pool.Exec(t.Context(), `UPDATE tenant.api_tokens SET expires_at = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	_, err = s.auth.WhoAmI(t.Context(), bearer(token, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
}

func TestTokenRequestsAreChecked(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	s.user(t, "grace", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.auth.CreateToken(t.Context(), bearer(token, &apiv1.CreateTokenRequest{Name: ""}))
	wantCode(t, err, connect.CodeInvalidArgument)
	_, err = s.auth.CreateToken(t.Context(), bearer(token, &apiv1.CreateTokenRequest{
		Name: "old", ExpiresAt: timestamppb.New(time.Now().Add(-time.Hour)),
	}))
	wantCode(t, err, connect.CodeInvalidArgument)

	theirs, err := s.auth.LoginForToken(t.Context(), connect.NewRequest(&apiv1.LoginForTokenRequest{
		Username: "grace", Password: pw, TokenName: "x",
	}))
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.auth.RevokeToken(t.Context(), bearer(token, &apiv1.RevokeTokenRequest{TokenId: theirs.Msg.GetInfo().GetTokenId()}))
	wantCode(t, err, connect.CodeNotFound)
	_, err = s.auth.Logout(t.Context(), bearer(token, &apiv1.LogoutRequest{}))
	wantCode(t, err, connect.CodeFailedPrecondition)
}

func TestSessionsEndWhenIdleOrOld(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)

	idle := s.login(t, "ada", pw)
	if _, err := s.pool.Exec(t.Context(), `UPDATE tenant.sessions SET last_seen_at = now() - interval '2 hours'`); err != nil {
		t.Fatal(err)
	}
	_, err := s.auth.WhoAmI(t.Context(), asBrowser(idle, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)

	old := s.login(t, "ada", pw)
	if _, err := s.pool.Exec(t.Context(), `UPDATE tenant.sessions SET expires_at = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	_, err = s.auth.WhoAmI(t.Context(), asBrowser(old, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)

	// Signing in again sweeps the dead sessions away.
	s.login(t, "ada", pw)
	var n int
	if err := s.pool.QueryRow(t.Context(), `SELECT count(*) FROM tenant.sessions`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("%d sessions stored, want only the live one", n)
	}
}

func TestChangingThePasswordEndsOtherSessions(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	here, elsewhere := s.login(t, "ada", pw), s.login(t, "ada", pw)
	token := s.token(t, "ada", pw)

	_, err := s.auth.ChangePassword(t.Context(), asBrowser(here, &apiv1.ChangePasswordRequest{
		CurrentPassword: "wrong password!!", NewPassword: "a new long password",
	}))
	wantCode(t, err, connect.CodeUnauthenticated)
	_, err = s.auth.ChangePassword(t.Context(), asBrowser(here, &apiv1.ChangePasswordRequest{
		CurrentPassword: pw, NewPassword: "short",
	}))
	wantCode(t, err, connect.CodeInvalidArgument)
	if _, err := s.auth.ChangePassword(t.Context(), asBrowser(here, &apiv1.ChangePasswordRequest{
		CurrentPassword: pw, NewPassword: "a new long password",
	})); err != nil {
		t.Fatal(err)
	}

	if _, err := s.auth.WhoAmI(t.Context(), asBrowser(here, &apiv1.WhoAmIRequest{})); err != nil {
		t.Fatalf("the session that changed the password: %v", err)
	}
	_, err = s.auth.WhoAmI(t.Context(), asBrowser(elsewhere, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	if _, err := s.auth.WhoAmI(t.Context(), bearer(token, &apiv1.WhoAmIRequest{})); err != nil {
		t.Fatalf("API tokens stay valid: %v", err)
	}
	s.login(t, "ada", "a new long password")
}

func TestOnlyAdminsManageUsers(t *testing.T) {
	s := newStack(t)
	s.user(t, "root", pw, tenant.RoleAdmin)
	s.user(t, "ada", pw, tenant.RoleMember)
	admin, member := s.token(t, "root", pw), s.token(t, "ada", pw)

	_, err := s.users.ListUsers(t.Context(), bearer(member, &apiv1.ListUsersRequest{}))
	wantCode(t, err, connect.CodePermissionDenied)

	created, err := s.users.CreateUser(t.Context(), bearer(admin, &apiv1.CreateUserRequest{
		Username: "grace", Password: pw, Role: apiv1.Role_ROLE_MEMBER,
	}))
	if err != nil {
		t.Fatal(err)
	}
	if created.Msg.GetUser().GetUsername() != "grace" {
		t.Fatalf("CreateUser: %v", created.Msg.GetUser())
	}
	for _, bad := range []*apiv1.CreateUserRequest{
		{Username: "grace", Password: pw, Role: apiv1.Role_ROLE_MEMBER},
		{Username: "Bad Name", Password: pw, Role: apiv1.Role_ROLE_MEMBER},
		{Username: "linus", Password: "short", Role: apiv1.Role_ROLE_MEMBER},
		{Username: "linus", Password: pw},
	} {
		_, err := s.users.CreateUser(t.Context(), bearer(admin, bad))
		if code := connect.CodeOf(err); code != connect.CodeAlreadyExists && code != connect.CodeInvalidArgument {
			t.Errorf("CreateUser(%v): %v", bad, err)
		}
	}
	listed, err := s.users.ListUsers(t.Context(), bearer(admin, &apiv1.ListUsersRequest{}))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(listed.Msg.GetUsers()); n != 3 {
		t.Fatalf("ListUsers: %d users, want 3", n)
	}
	_, err = s.users.SetUserDisabled(t.Context(), bearer(admin, &apiv1.SetUserDisabledRequest{Username: "root", Disabled: true}))
	wantCode(t, err, connect.CodeFailedPrecondition)
	_, err = s.users.SetUserDisabled(t.Context(), bearer(admin, &apiv1.SetUserDisabledRequest{Username: "nobody", Disabled: true}))
	wantCode(t, err, connect.CodeNotFound)
}

func TestDisablingAUserStopsTheirSessionsAndTokens(t *testing.T) {
	s := newStack(t)
	s.user(t, "root", pw, tenant.RoleAdmin)
	s.user(t, "ada", pw, tenant.RoleMember)
	admin := s.token(t, "root", pw)
	session, token := s.login(t, "ada", pw), s.token(t, "ada", pw)

	if _, err := s.users.SetUserDisabled(t.Context(), bearer(admin, &apiv1.SetUserDisabledRequest{Username: "ada", Disabled: true})); err != nil {
		t.Fatal(err)
	}
	_, err := s.auth.WhoAmI(t.Context(), asBrowser(session, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	_, err = s.auth.WhoAmI(t.Context(), bearer(token, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)

	if _, err := s.users.SetUserDisabled(t.Context(), bearer(admin, &apiv1.SetUserDisabledRequest{Username: "ada", Disabled: false})); err != nil {
		t.Fatal(err)
	}
	if _, err := s.auth.WhoAmI(t.Context(), bearer(token, &apiv1.WhoAmIRequest{})); err != nil {
		t.Fatalf("a token of a user enabled again: %v", err)
	}
	// The session ended for good when the user was disabled.
	_, err = s.auth.WhoAmI(t.Context(), asBrowser(session, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
}

func TestResettingAPasswordEndsTheUsersSessions(t *testing.T) {
	s := newStack(t)
	s.user(t, "root", pw, tenant.RoleAdmin)
	s.user(t, "ada", pw, tenant.RoleMember)
	admin, session := s.token(t, "root", pw), s.login(t, "ada", pw)

	if _, err := s.users.ResetPassword(t.Context(), bearer(admin, &apiv1.ResetPasswordRequest{Username: "ada", NewPassword: "a brand new password"})); err != nil {
		t.Fatal(err)
	}
	_, err := s.auth.WhoAmI(t.Context(), asBrowser(session, &apiv1.WhoAmIRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	s.login(t, "ada", "a brand new password")
}

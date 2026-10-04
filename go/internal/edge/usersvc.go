package edge

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

type UserService struct {
	store *tenant.Store
	auth  *AuthService
	log   *slog.Logger
}

func requireAdmin(ctx context.Context) (principal, error) {
	caller := callerOf(ctx)
	if caller.user.Role != tenant.RoleAdmin {
		return caller, connect.NewError(connect.CodePermissionDenied, fmt.Errorf(
			"user %q is a member; managing users needs the admin role", caller.user.Username))
	}
	return caller, nil
}

func (s *UserService) notFound(username string) error {
	return connect.NewError(connect.CodeNotFound, fmt.Errorf("no user %q; ListUsers shows them all", username))
}

func (s *UserService) CreateUser(ctx context.Context, req *connect.Request[apiv1.CreateUserRequest]) (*connect.Response[apiv1.CreateUserResponse], error) {
	if _, err := requireAdmin(ctx); err != nil {
		return nil, err
	}
	var role tenant.Role
	switch req.Msg.GetRole() {
	case apiv1.Role_ROLE_ADMIN:
		role = tenant.RoleAdmin
	case apiv1.Role_ROLE_MEMBER:
		role = tenant.RoleMember
	default:
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"role %s is not one a user can have; use ROLE_MEMBER or ROLE_ADMIN", req.Msg.GetRole()))
	}
	if err := tenant.CheckUsername(req.Msg.GetUsername()); err != nil {
		return nil, connect.NewError(connect.CodeInvalidArgument, err)
	}
	if err := tenant.CheckPassword(req.Msg.GetPassword()); err != nil {
		return nil, connect.NewError(connect.CodeInvalidArgument, err)
	}
	hash, err := s.auth.hash(ctx, req.Msg.GetPassword())
	if err != nil {
		return nil, err
	}
	user, err := s.store.CreateUser(ctx, req.Msg.GetUsername(), hash, role)
	if errors.Is(err, tenant.ErrExists) {
		return nil, connect.NewError(connect.CodeAlreadyExists, fmt.Errorf("user %q already exists; pick another name", req.Msg.GetUsername()))
	}
	if err != nil {
		return nil, internalError(s.log, "create user", err)
	}
	return connect.NewResponse(&apiv1.CreateUserResponse{User: userProto(user)}), nil
}

func (s *UserService) ListUsers(ctx context.Context, _ *connect.Request[apiv1.ListUsersRequest]) (*connect.Response[apiv1.ListUsersResponse], error) {
	if _, err := requireAdmin(ctx); err != nil {
		return nil, err
	}
	users, err := s.store.ListUsers(ctx)
	if err != nil {
		return nil, internalError(s.log, "list users", err)
	}
	res := &apiv1.ListUsersResponse{Users: make([]*apiv1.User, len(users))}
	for i, u := range users {
		res.Users[i] = userProto(u)
	}
	return connect.NewResponse(res), nil
}

func (s *UserService) SetUserDisabled(ctx context.Context, req *connect.Request[apiv1.SetUserDisabledRequest]) (*connect.Response[apiv1.SetUserDisabledResponse], error) {
	caller, err := requireAdmin(ctx)
	if err != nil {
		return nil, err
	}
	if req.Msg.GetDisabled() && req.Msg.GetUsername() == caller.user.Username {
		return nil, connect.NewError(connect.CodeFailedPrecondition, errors.New(
			"an admin cannot disable themselves; ask another admin"))
	}
	user, err := s.store.SetDisabled(ctx, req.Msg.GetUsername(), req.Msg.GetDisabled())
	if errors.Is(err, tenant.ErrNotFound) {
		return nil, s.notFound(req.Msg.GetUsername())
	}
	if err != nil {
		return nil, internalError(s.log, "disable user", err)
	}
	return connect.NewResponse(&apiv1.SetUserDisabledResponse{User: userProto(user)}), nil
}

func (s *UserService) ResetPassword(ctx context.Context, req *connect.Request[apiv1.ResetPasswordRequest]) (*connect.Response[apiv1.ResetPasswordResponse], error) {
	if _, err := requireAdmin(ctx); err != nil {
		return nil, err
	}
	if err := tenant.CheckPassword(req.Msg.GetNewPassword()); err != nil {
		return nil, connect.NewError(connect.CodeInvalidArgument, err)
	}
	user, err := s.store.UserByName(ctx, req.Msg.GetUsername())
	if errors.Is(err, tenant.ErrNotFound) {
		return nil, s.notFound(req.Msg.GetUsername())
	}
	if err != nil {
		return nil, internalError(s.log, "look up user", err)
	}
	hash, err := s.auth.hash(ctx, req.Msg.GetNewPassword())
	if err != nil {
		return nil, err
	}
	if err := s.store.SetPassword(ctx, user.ID, hash, nil); err != nil {
		return nil, internalError(s.log, "store password", err)
	}
	return connect.NewResponse(&apiv1.ResetPasswordResponse{}), nil
}

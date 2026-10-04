package cli

import (
	"fmt"
	"os"
	"text/tabwriter"
	"time"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/types/known/timestamppb"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

func (a *app) loginCommand() *cobra.Command {
	var username, token, tokenName string
	cmd := &cobra.Command{
		Use:   "login",
		Short: "Sign in to edge and save an API token for later commands",
		Long: "Signs in with a username and password and saves a new API token, or saves a token\n" +
			"you already have (--token). On a terminal the password is asked without echo;\n" +
			"otherwise it is read from the first line of standard input.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, path, err := a.config()
			if err != nil {
				return err
			}
			c, err := newClients(cfg.Endpoint, token)
			if err != nil {
				return err
			}
			cfg.Endpoint = c.endpoint
			if token != "" {
				me, err := c.auth.WhoAmI(cmd.Context(), connect.NewRequest(&apiv1.WhoAmIRequest{}))
				if err != nil {
					return explain("check the token", err)
				}
				cfg.Token, cfg.TokenID = token, ""
				if err := saveConfig(path, cfg); err != nil {
					return err
				}
				_, err = fmt.Fprintf(a.out, "signed in to %s as %s; token saved to %s\n", cfg.Endpoint, me.Msg.GetUser().GetUsername(), path)
				return err
			}
			if username == "" {
				if _, ok := a.stdinIsTerminal(); !ok {
					return fmt.Errorf("give --username when the password comes from standard input")
				}
				_, _ = fmt.Fprint(a.err, "Username: ")
				if username, err = a.readLine(); err != nil {
					return err
				}
			}
			password, err := a.readSecret("Password: ")
			if err != nil {
				return err
			}
			if tokenName == "" {
				host, _ := os.Hostname() // a token name is a label; an unknown host still signs in
				tokenName = "swarm CLI on " + host
			}
			res, err := c.auth.LoginForToken(cmd.Context(), connect.NewRequest(&apiv1.LoginForTokenRequest{
				Username: username, Password: password, TokenName: tokenName,
			}))
			if err != nil {
				return explain("sign in", err)
			}
			cfg.Token, cfg.TokenID = res.Msg.GetToken(), res.Msg.GetInfo().GetTokenId()
			if err := saveConfig(path, cfg); err != nil {
				return err
			}
			_, err = fmt.Fprintf(a.out, "signed in to %s as %s; token %q saved to %s\n", cfg.Endpoint, username, tokenName, path)
			return err
		},
	}
	cmd.Flags().StringVarP(&username, "username", "u", "", "username (asked when omitted)")
	cmd.Flags().StringVar(&token, "token", "", "save this API token instead of signing in with a password")
	cmd.Flags().StringVar(&tokenName, "token-name", "", `name of the new token (default "swarm CLI on <hostname>")`)
	return cmd
}

func (a *app) logoutCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "logout",
		Short: "Revoke the token `swarm login` made and forget it",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, path, err := a.config()
			if err != nil {
				return err
			}
			if cfg.Token == "" {
				_, err := fmt.Fprintln(a.out, "not signed in")
				return err
			}
			note := "token revoked"
			if cfg.TokenID != "" {
				c, err := newClients(cfg.Endpoint, cfg.Token)
				if err != nil {
					return err
				}
				if _, err := c.auth.RevokeToken(cmd.Context(), connect.NewRequest(&apiv1.RevokeTokenRequest{TokenId: cfg.TokenID})); err != nil {
					return explain("revoke the token", err)
				}
			} else {
				note = "token forgotten; it was not made by `swarm login`, so it stays valid until you revoke it in the console or with `swarm token revoke`"
			}
			cfg.Token, cfg.TokenID = "", ""
			if err := saveConfig(path, cfg); err != nil {
				return err
			}
			_, err = fmt.Fprintln(a.out, note)
			return err
		},
	}
}

func (a *app) whoamiCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "whoami",
		Short: "Show who the saved token signs in as",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.auth.WhoAmI(cmd.Context(), connect.NewRequest(&apiv1.WhoAmIRequest{}))
			if err != nil {
				return explain("whoami", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg.GetUser())
			}
			_, err = fmt.Fprintf(a.out, "%s (%s) at %s\n", res.Msg.GetUser().GetUsername(), roleName(res.Msg.GetUser().GetRole()), c.endpoint)
			return err
		},
	}
}

func roleName(r apiv1.Role) string {
	switch r {
	case apiv1.Role_ROLE_ADMIN:
		return "admin"
	case apiv1.Role_ROLE_MEMBER:
		return "member"
	}
	return r.String()
}

func (a *app) tokenCommand() *cobra.Command {
	cmd := &cobra.Command{Use: "token", Short: "Manage your API tokens"}
	var expires time.Duration
	create := &cobra.Command{
		Use:   "create NAME",
		Short: "Create an API token, such as one for CI; it is printed once",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			req := &apiv1.CreateTokenRequest{Name: args[0]}
			if expires > 0 {
				req.ExpiresAt = timestamppb.New(time.Now().Add(expires))
			}
			res, err := c.auth.CreateToken(cmd.Context(), connect.NewRequest(req))
			if err != nil {
				return explain("create token", err)
			}
			_, err = fmt.Fprintf(a.out, "%s\n(token %s; shown only now)\n", res.Msg.GetToken(), res.Msg.GetInfo().GetTokenId())
			return err
		},
	}
	create.Flags().DurationVar(&expires, "expires", 0, "expire after this long, such as 720h (default: never)")
	list := &cobra.Command{
		Use:   "list",
		Short: "List your API tokens, revoked ones included",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.auth.ListTokens(cmd.Context(), connect.NewRequest(&apiv1.ListTokensRequest{}))
			if err != nil {
				return explain("list tokens", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			tw := tabwriter.NewWriter(a.out, 0, 0, 2, ' ', 0)
			_, _ = fmt.Fprintln(tw, "ID\tNAME\tCREATED\tEXPIRES\tLAST USED\tREVOKED")
			for _, t := range res.Msg.GetTokens() {
				_, _ = fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\t%s\n", t.GetTokenId(), t.GetName(),
					when(t.GetCreatedAt()), when(t.GetExpiresAt()), when(t.GetLastUsedAt()), when(t.GetRevokedAt()))
			}
			return tw.Flush()
		},
	}
	revoke := &cobra.Command{
		Use:   "revoke ID",
		Short: "Revoke one of your API tokens by its id (tok_…)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			if _, err := c.auth.RevokeToken(cmd.Context(), connect.NewRequest(&apiv1.RevokeTokenRequest{TokenId: args[0]})); err != nil {
				return explain("revoke token", err)
			}
			_, err = fmt.Fprintf(a.out, "revoked %s\n", args[0])
			return err
		},
	}
	cmd.AddCommand(create, list, revoke)
	return cmd
}

func (a *app) userCommand() *cobra.Command {
	cmd := &cobra.Command{Use: "user", Short: "Manage users (admins only)"}
	var admin bool
	create := &cobra.Command{
		Use:   "create USERNAME",
		Short: "Create a user; the password is asked, or read from standard input",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			password, err := a.newPassword()
			if err != nil {
				return err
			}
			role := apiv1.Role_ROLE_MEMBER
			if admin {
				role = apiv1.Role_ROLE_ADMIN
			}
			res, err := c.users.CreateUser(cmd.Context(), connect.NewRequest(&apiv1.CreateUserRequest{
				Username: args[0], Password: password, Role: role,
			}))
			if err != nil {
				return explain("create user", err)
			}
			_, err = fmt.Fprintf(a.out, "created %s %s\n", roleName(res.Msg.GetUser().GetRole()), res.Msg.GetUser().GetUsername())
			return err
		},
	}
	create.Flags().BoolVar(&admin, "admin", false, "give the user the admin role")
	list := &cobra.Command{
		Use:   "list",
		Short: "List users",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.users.ListUsers(cmd.Context(), connect.NewRequest(&apiv1.ListUsersRequest{}))
			if err != nil {
				return explain("list users", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			tw := tabwriter.NewWriter(a.out, 0, 0, 2, ' ', 0)
			_, _ = fmt.Fprintln(tw, "USERNAME\tROLE\tDISABLED\tCREATED")
			for _, u := range res.Msg.GetUsers() {
				_, _ = fmt.Fprintf(tw, "%s\t%s\t%t\t%s\n", u.GetUsername(), roleName(u.GetRole()), u.GetDisabled(), when(u.GetCreatedAt()))
			}
			return tw.Flush()
		},
	}
	setDisabled := func(use, short string, disabled bool) *cobra.Command {
		return &cobra.Command{
			Use:   use + " USERNAME",
			Short: short,
			Args:  cobra.ExactArgs(1),
			RunE: func(cmd *cobra.Command, args []string) error {
				c, err := a.signedIn()
				if err != nil {
					return err
				}
				if _, err := c.users.SetUserDisabled(cmd.Context(), connect.NewRequest(&apiv1.SetUserDisabledRequest{
					Username: args[0], Disabled: disabled,
				})); err != nil {
					return explain(use+" user", err)
				}
				_, err = fmt.Fprintf(a.out, "%sd %s\n", use, args[0])
				return err
			},
		}
	}
	reset := &cobra.Command{
		Use:   "reset-password USERNAME",
		Short: "Set a user's password and end their sessions",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			password, err := a.newPassword()
			if err != nil {
				return err
			}
			if _, err := c.users.ResetPassword(cmd.Context(), connect.NewRequest(&apiv1.ResetPasswordRequest{
				Username: args[0], NewPassword: password,
			})); err != nil {
				return explain("reset password", err)
			}
			_, err = fmt.Fprintf(a.out, "password of %s reset; their sessions ended\n", args[0])
			return err
		},
	}
	cmd.AddCommand(create, list,
		setDisabled("disable", "Disable a user: their sessions end and their tokens stop working", true),
		setDisabled("enable", "Enable a disabled user", false),
		reset)
	return cmd
}

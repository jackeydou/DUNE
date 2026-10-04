package cli

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// app is one invocation's state: where it reads and writes, and the endpoint flag.
type app struct {
	in       io.Reader
	out, err io.Writer
	endpoint string
	asJSON   bool
}

// NewRoot builds the `swarm` command.
func NewRoot(in io.Reader, out, errOut io.Writer) *cobra.Command {
	a := &app{in: in, out: out, err: errOut}
	root := &cobra.Command{
		Use:   "swarm",
		Short: "SwarmEval's command line: submit and follow evaluation runs through edge",
		Long: "swarm talks to edge, SwarmEval's public entry point, with an API token from `swarm login`.\n" +
			"The endpoint and token are read from ~/.config/swarm/config.yaml (or $SWARM_CONFIG),\n" +
			"and $SWARM_ENDPOINT and $SWARM_TOKEN override them. For an edge with a self-signed\n" +
			"certificate, `swarm login --ca-file` or $SWARM_CA_FILE names the certificate to trust.",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.SetIn(in)
	root.SetOut(out)
	root.SetErr(errOut)
	root.PersistentFlags().StringVar(&a.endpoint, "endpoint", "", "edge's address, such as https://swarm.example.com (default: the config file's)")
	root.PersistentFlags().BoolVar(&a.asJSON, "json", false, "print results as JSON where a command prints records")
	root.AddCommand(
		a.loginCommand(), a.logoutCommand(), a.whoamiCommand(), a.tokenCommand(), a.userCommand(),
		a.runCommand(), a.runsCommand(), a.eventsCommand(), a.replayCommand(), a.caseCommand(),
	)
	return root
}

// config loads the config with --endpoint applied.
func (a *app) config() (Config, string, error) {
	cfg, path, err := loadConfig()
	if err != nil {
		return cfg, path, err
	}
	if a.endpoint != "" {
		cfg.Endpoint = a.endpoint
	}
	return cfg, path, nil
}

// signedIn returns clients for a caller that has a token.
func (a *app) signedIn() (*clients, error) {
	cfg, _, err := a.config()
	if err != nil {
		return nil, err
	}
	if cfg.Token == "" {
		return nil, errors.New("not signed in: run `swarm login`, or set " + envToken)
	}
	return newClients(cfg)
}

// stdinIsTerminal reports whether prompts can hide what is typed.
func (a *app) stdinIsTerminal() (int, bool) {
	f, ok := a.in.(*os.File)
	if !ok {
		return 0, false
	}
	fd := int(f.Fd())
	return fd, term.IsTerminal(fd)
}

// readSecret prompts without echo on a terminal; otherwise it reads one line from stdin.
func (a *app) readSecret(prompt string) (string, error) {
	if fd, ok := a.stdinIsTerminal(); ok {
		_, _ = fmt.Fprint(a.err, prompt)
		b, err := term.ReadPassword(fd)
		_, _ = fmt.Fprintln(a.err)
		return string(b), err
	}
	return a.readLine()
}

func (a *app) readLine() (string, error) {
	line, err := bufio.NewReader(a.in).ReadString('\n')
	if err != nil && line == "" {
		return "", fmt.Errorf("read standard input: %w", err)
	}
	return strings.TrimRight(line, "\r\n"), nil
}

// newPassword asks twice on a terminal, once from a pipe.
func (a *app) newPassword() (string, error) {
	first, err := a.readSecret("New password: ")
	if err != nil {
		return "", err
	}
	if _, ok := a.stdinIsTerminal(); !ok {
		return first, nil
	}
	second, err := a.readSecret("Again: ")
	if err != nil {
		return "", err
	}
	if first != second {
		return "", errors.New("the passwords differ; run the command again")
	}
	return first, nil
}

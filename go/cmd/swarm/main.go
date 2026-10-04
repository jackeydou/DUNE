// Command swarm is SwarmEval's command line, a client of edge (docs/services/edge.md#swarm-cli).
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/jackeydou/DUNE/go/internal/cli"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "swarm:", err)
		os.Exit(1)
	}
}

func run() error {
	// Ctrl-C cancels the command's context: a follow stops, and the runs go on.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return cli.NewRoot(os.Stdin, os.Stdout, os.Stderr).ExecuteContext(ctx)
}

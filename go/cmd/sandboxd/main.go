// Command sandboxd serves swarmeval.sandbox.v1.SandboxService on top of docker.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"google.golang.org/grpc"

	"github.com/jackeydou/DUNE/go/internal/driver/docker"
	sandboxv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/sandbox/v1"
	"github.com/jackeydou/DUNE/go/internal/sandboxd"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("sandboxd stopped", "err", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	listen := flag.String("listen", "127.0.0.1:7071", "address to serve gRPC on. Only workers may reach it")
	stateDir := flag.String("state-dir", "", "directory for sandbox key paths; the docker daemon must see it at the same path (required)")
	runtime := flag.String("runtime", "runc", "container runtime: runc, runsc, or auto (runsc when docker offers it)")
	flag.Parse()

	if *stateDir == "" {
		return errors.New("--state-dir is required: sandboxes' key paths live there, bind-mounted from the host")
	}
	switch *runtime {
	case "runc", "runsc", "auto":
	default:
		return fmt.Errorf("--runtime %q: want runc, runsc, or auto", *runtime)
	}
	if err := os.MkdirAll(*stateDir, 0o755); err != nil {
		return fmt.Errorf("create state dir %s: %w", *stateDir, err)
	}
	// Bind mount sources must be real paths (on macOS /tmp is a symlink).
	resolved, err := filepath.EvalSymlinks(*stateDir)
	if err != nil {
		return fmt.Errorf("resolve state dir %s: %w", *stateDir, err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	drv, err := docker.New(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = drv.Close() }() // process exit follows

	cfg := sandboxd.DefaultConfig(resolved)
	cfg.Runtime = *runtime
	svc := sandboxd.New(cfg, drv, log)

	lis, err := net.Listen("tcp", *listen)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", *listen, err)
	}
	server := grpc.NewServer()
	sandboxv1.RegisterSandboxServiceServer(server, sandboxd.NewServer(svc, log))

	go func() {
		<-ctx.Done()
		server.GracefulStop()
	}()
	log.Info("sandboxd serving", "listen", lis.Addr().String(), "state_dir", resolved, "runtime", *runtime)
	return server.Serve(lis)
}

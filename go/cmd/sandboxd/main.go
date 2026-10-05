// Command sandboxd serves swarmeval.sandbox.v1.SandboxService on top of docker.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/jackeydou/DUNE/go/internal/driver/docker"
	sandboxv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/sandbox/v1"
	"github.com/jackeydou/DUNE/go/internal/mtls"
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
	listen := flag.String("listen", "127.0.0.1:7071", "address to serve gRPC on. Without --mtls-cert it must be a loopback address")
	stateDir := flag.String("state-dir", "", "directory for sandbox key paths; the docker daemon must see it at the same path (required)")
	runtime := flag.String("runtime", "auto", "container runtime: runc, runsc, or auto (runsc when docker offers it)")
	network := flag.String("sandbox-network", "none", "sandbox networking: none (loopback only), or per-sandbox (a network per sandbox for net-gateway, which is not built, so it reaches nothing)")
	subnets := flag.String("sandbox-subnets", "10.231.0.0/16", "IPv4 pool that per-sandbox networks take a /28 each from")
	var identity mtls.Files
	identity.Flags(flag.CommandLine)
	flag.Parse()

	options, err := serverOptions(identity, *listen, log)
	if err != nil {
		return err
	}
	if *stateDir == "" {
		return errors.New("--state-dir is required: sandboxes' key paths live there, bind-mounted from the host")
	}
	switch *runtime {
	case "runc", "runsc", "auto":
	default:
		return fmt.Errorf("--runtime %q: want runc, runsc, or auto", *runtime)
	}
	switch sandboxd.NetworkMode(*network) {
	case sandboxd.NetworkNone, sandboxd.NetworkPerSandbox:
	default:
		return fmt.Errorf("--sandbox-network %q: want none or per-sandbox", *network)
	}
	pool, err := netip.ParsePrefix(*subnets)
	if err != nil || !pool.Addr().Is4() || pool.Bits() > 28 {
		return fmt.Errorf("--sandbox-subnets %q: want an IPv4 prefix of /28 or wider, such as 10.231.0.0/16", *subnets)
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
	cfg.Network = sandboxd.NetworkMode(*network)
	cfg.SubnetPool = pool.Masked()
	svc := sandboxd.New(cfg, drv, log)

	lis, err := net.Listen("tcp", *listen)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", *listen, err)
	}
	server := grpc.NewServer(options...)
	sandboxv1.RegisterSandboxServiceServer(server, sandboxd.NewServer(svc, log))

	go func() {
		<-ctx.Done()
		server.GracefulStop()
	}()
	log.Info("sandboxd serving", "listen", lis.Addr().String(), "mtls", identity.Enabled(), "state_dir", resolved, "runtime", *runtime, "sandbox_network", *network, "sandbox_subnets", cfg.SubnetPool.String())
	return server.Serve(lis)
}

// serverOptions makes sandboxd accept only workers: over mutual TLS when it has a certificate,
// and otherwise only on a loopback address, since the API has no other authentication.
func serverOptions(identity mtls.Files, listen string, log *slog.Logger) ([]grpc.ServerOption, error) {
	if err := identity.Check(); err != nil {
		return nil, err
	}
	if !identity.Enabled() {
		if !mtls.IsLoopback(listen) {
			return nil, fmt.Errorf("--listen %s is not a loopback address, and sandboxd has no certificate: anyone who reached it could run commands in sandboxes. Give --mtls-cert, --mtls-key, and --mtls-ca, or listen on 127.0.0.1", listen)
		}
		return nil, nil
	}
	config, err := identity.Server(log, mtls.Worker)
	if err != nil {
		return nil, err
	}
	return []grpc.ServerOption{grpc.Creds(credentials.NewTLS(config))}, nil
}

package main

import (
	"context"
	"io"
	"log/slog"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	"github.com/jackeydou/DUNE/go/internal/certs"
	sandboxv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/sandbox/v1"
	"github.com/jackeydou/DUNE/go/internal/mtls"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

func TestWithoutACertificateSandboxdListensOnLoopbackOnly(t *testing.T) {
	for _, listen := range []string{"0.0.0.0:7071", ":7071", "10.0.0.5:7071"} {
		if _, err := serverOptions(mtls.Files{}, listen, quiet); err == nil || !strings.Contains(err.Error(), "not a loopback address") {
			t.Errorf("--listen %s without a certificate: %v", listen, err)
		}
	}
	if options, err := serverOptions(mtls.Files{}, "127.0.0.1:7071", quiet); err != nil || options != nil {
		t.Errorf("loopback without a certificate: %v, %v", options, err)
	}
	if _, err := serverOptions(mtls.Files{Cert: "tls.crt"}, "127.0.0.1:7071", quiet); err == nil {
		t.Error("a certificate without its key and CA was accepted")
	}
}

func TestWithACertificateSandboxdServesOnlyWorkers(t *testing.T) {
	dir := t.TempDir()
	if err := certs.Generate(dir, certs.Options{Now: time.Now()}); err != nil {
		t.Fatal(err)
	}
	options, err := serverOptions(certs.Paths(dir, mtls.Sandboxd), "0.0.0.0:7071", quiet)
	if err != nil {
		t.Fatal(err)
	}
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(options...)
	sandboxv1.RegisterSandboxServiceServer(server, sandboxv1.UnimplementedSandboxServiceServer{})
	go func() { _ = server.Serve(lis) }()
	t.Cleanup(server.Stop)

	// The stand-in service answers UNIMPLEMENTED, so that code means the call got through.
	call := func(creds credentials.TransportCredentials) codes.Code {
		conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(creds))
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = conn.Close() }()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, err = sandboxv1.NewSandboxServiceClient(conn).DestroyRun(ctx, &sandboxv1.DestroyRunRequest{RunId: "r"})
		return status.Code(err)
	}
	as := func(service string) credentials.TransportCredentials {
		config, err := certs.Paths(dir, service).Client(mtls.Sandboxd)
		if err != nil {
			t.Fatal(err)
		}
		return credentials.NewTLS(config)
	}
	if got := call(as(mtls.Worker)); got != codes.Unimplemented {
		t.Errorf("a worker's call ended %s, want it to reach the service", got)
	}
	for _, service := range []string{mtls.Edge, mtls.Control, mtls.ModelGateway, mtls.Analysis, mtls.Operator} {
		if got := call(as(service)); got != codes.Unavailable {
			t.Errorf("%s's call ended %s, want the connection refused", service, got)
		}
	}
	if got := call(insecure.NewCredentials()); got != codes.Unavailable {
		t.Errorf("a plain-text call ended %s, want the connection refused", got)
	}
}

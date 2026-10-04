package certs

// The handshake rules of internal/mtls, tested here because they need certificates and mtls
// cannot import this package.

import (
	"crypto/tls"
	"io"
	"log/slog"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/jackeydou/DUNE/go/internal/mtls"
)

// serve accepts connections under config and answers each completed handshake with "ok".
func serve(t *testing.T, config *tls.Config) string {
	t.Helper()
	lis, err := tls.Listen("tcp", "127.0.0.1:0", config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = lis.Close() })
	go func() {
		for {
			conn, err := lis.Accept()
			if err != nil {
				return
			}
			go func() {
				defer func() { _ = conn.Close() }()
				if conn.(*tls.Conn).Handshake() == nil {
					_, _ = conn.Write([]byte("ok"))
				}
			}()
		}
	}()
	return lis.Addr().String()
}

// call reports whether a client under config got the server's answer. In TLS 1.3 a server
// that refuses the client's certificate says so only after the client's handshake returned,
// so the refusal shows up on the first read.
func call(address string, config *tls.Config) error {
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", address, config)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()
	_, err = io.ReadFull(conn, make([]byte, 2))
	return err
}

func TestServerAcceptsOnlyListedServices(t *testing.T) {
	dir, other := t.TempDir(), t.TempDir()
	for _, d := range []string{dir, other} {
		if err := Generate(d, Options{Now: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	var refusals strings.Builder
	log := slog.New(slog.NewTextHandler(&refusals, nil))
	server, err := Paths(dir, mtls.Sandboxd).Server(log, mtls.Worker)
	if err != nil {
		t.Fatal(err)
	}
	address := serve(t, server)

	client := func(dir, service string) *tls.Config {
		config, err := Paths(dir, service).Client(mtls.Sandboxd)
		if err != nil {
			t.Fatal(err)
		}
		return config
	}
	if err := call(address, client(dir, mtls.Worker)); err != nil {
		t.Fatalf("the worker was refused: %v", err)
	}
	if err := call(address, client(dir, mtls.Edge)); err == nil {
		t.Error("edge, which is not on sandboxd's list, was accepted")
	} else if !strings.Contains(refusals.String(), `service \"edge\" may not call this one`) {
		t.Errorf("the refusal was not logged with its reason: %q", refusals.String())
	}
	if err := call(address, client(other, mtls.Worker)); err == nil {
		t.Error("a worker certificate from another CA was accepted")
	}
	anonymous := client(dir, mtls.Worker)
	anonymous.Certificates = nil
	if err := call(address, anonymous); err == nil {
		t.Error("a client without a certificate was accepted")
	}
}

func TestClientConnectsOnlyToTheNamedService(t *testing.T) {
	dir := t.TempDir()
	if err := Generate(dir, Options{Now: time.Now()}); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	// model-gateway accepts workers too, so only the client's own check stands between a
	// worker and the wrong service.
	server, err := Paths(dir, mtls.ModelGateway).Server(log, mtls.Worker)
	if err != nil {
		t.Fatal(err)
	}
	address := serve(t, server)
	config, err := Paths(dir, mtls.Worker).Client(mtls.Sandboxd)
	if err != nil {
		t.Fatal(err)
	}
	if err := call(address, config); err == nil || !strings.Contains(err.Error(), `service "model-gateway", want "sandboxd"`) {
		t.Fatalf("a client that wanted sandboxd talked to model-gateway: %v", err)
	}
}

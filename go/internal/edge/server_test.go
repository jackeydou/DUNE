package edge

import (
	"context"
	"crypto/tls"
	"io"
	"log/slog"
	"net/http/httptest"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/certs"
	analysisv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
	"github.com/jackeydou/DUNE/go/internal/mtls"
)

// tlsControl is a Control API that implements nothing, behind config: UNIMPLEMENTED means a
// call got through.
func tlsControl(t *testing.T, config *tls.Config) string {
	t.Helper()
	_, handler := controlv1connect.NewControlServiceHandler(controlv1connect.UnimplementedControlServiceHandler{})
	srv := httptest.NewUnstartedServer(handler)
	srv.EnableHTTP2 = true
	srv.Config.ErrorLog = slog.NewLogLogger(slog.NewTextHandler(io.Discard, nil), slog.LevelError)
	srv.TLS = config
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().String()
}

func getRun(t *testing.T, address string, identity mtls.Files) connect.Code {
	t.Helper()
	client, err := NewControlClient(address, identity)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err = client.GetRun(ctx, connect.NewRequest(&controlv1.GetRunRequest{RunId: "r"}))
	return connect.CodeOf(err)
}

func TestControlClientConnectsAsEdgeToControlOnly(t *testing.T) {
	dir := t.TempDir()
	if _, err := certs.Generate(dir, certs.Options{Now: time.Now()}); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	serverConfig := func(service string) *tls.Config {
		config, err := certs.Paths(dir, service).Server(log, mtls.Edge, mtls.Operator)
		if err != nil {
			t.Fatal(err)
		}
		return config
	}

	control := tlsControl(t, serverConfig(mtls.Control))
	if got := getRun(t, control, certs.Paths(dir, mtls.Edge)); got != connect.CodeUnimplemented {
		t.Errorf("edge's call ended %s, want it to reach the Control API", got)
	}
	if got := getRun(t, control, certs.Paths(dir, mtls.Worker)); got != connect.CodeUnavailable {
		t.Errorf("a call with the worker's certificate ended %s, want the connection refused", got)
	}
	if got := getRun(t, control, mtls.Files{}); got != connect.CodeUnavailable {
		t.Errorf("a plain-text call ended %s, want the connection refused", got)
	}

	// Another service of the same CA at the Control API's address: edge must not send it
	// the caller's requests.
	impostor := tlsControl(t, serverConfig(mtls.Sandboxd))
	if got := getRun(t, impostor, certs.Paths(dir, mtls.Edge)); got != connect.CodeUnavailable {
		t.Errorf("edge talked to a server holding sandboxd's certificate: %s", got)
	}
}

func TestAnalysisClientConnectsAsEdgeToAnalysisOnly(t *testing.T) {
	dir := t.TempDir()
	if _, err := certs.Generate(dir, certs.Options{Now: time.Now()}); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	// Both stand-ins accept edge; only the client's own check tells them apart.
	serve := func(service string) string {
		config, err := certs.Paths(dir, service).Server(log, mtls.Edge)
		if err != nil {
			t.Fatal(err)
		}
		_, handler := analysisv1connect.NewAnalysisServiceHandler(analysisv1connect.UnimplementedAnalysisServiceHandler{})
		srv := httptest.NewUnstartedServer(handler)
		srv.EnableHTTP2 = true
		srv.Config.ErrorLog = slog.NewLogLogger(slog.NewTextHandler(io.Discard, nil), slog.LevelError)
		srv.TLS = config
		srv.StartTLS()
		t.Cleanup(srv.Close)
		return srv.Listener.Addr().String()
	}
	getJob := func(address string) connect.Code {
		client, err := NewAnalysisClient(address, certs.Paths(dir, mtls.Edge))
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_, err = client.GetJob(ctx, connect.NewRequest(&analysisv1.GetJobRequest{JobId: "j"}))
		return connect.CodeOf(err)
	}
	if got := getJob(serve(mtls.Analysis)); got != connect.CodeUnimplemented {
		t.Errorf("edge's call ended %s, want it to reach the analysis service", got)
	}
	if got := getJob(serve(mtls.Control)); got != connect.CodeUnavailable {
		t.Errorf("edge sent an analysis call to a server holding control's certificate: %s", got)
	}
}

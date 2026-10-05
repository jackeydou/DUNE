package edge

import (
	"log/slog"
	"net/http"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
	"github.com/jackeydou/DUNE/go/internal/mtls"
)

// NewHandler serves the public API. Every procedure but sign-in runs behind the authenticator.
// analysis is nil on a deployment without an analysis service; its calls are then
// Unimplemented.
func NewHandler(cfg Config, store *tenant.Store, control controlv1connect.ControlServiceClient, analysis analysisv1connect.AnalysisServiceClient, log *slog.Logger) http.Handler {
	interceptors := connect.WithInterceptors(&authenticator{store: store, cfg: cfg, log: log})
	// Account calls carry a few short strings; only run submissions and case writes carry
	// bundles.
	small := connect.WithReadMaxBytes(maxAccountRequestBytes)
	large := connect.WithReadMaxBytes(maxRequestBytes)
	auth := newAuthService(store, cfg, log)
	mux := http.NewServeMux()
	mux.Handle(apiv1connect.NewAuthServiceHandler(auth, interceptors, small))
	mux.Handle(apiv1connect.NewUserServiceHandler(&UserService{store: store, auth: auth, log: log}, interceptors, small))
	mux.Handle(apiv1connect.NewRunServiceHandler(&RunService{control: control, log: log}, interceptors, large))
	mux.Handle(apiv1connect.NewCaseServiceHandler(&CaseService{control: control, log: log}, interceptors, large))
	// A rule set or a SQL statement, never a bundle.
	medium := connect.WithReadMaxBytes(maxAnalysisRequestBytes)
	mux.Handle(apiv1connect.NewAnalysisServiceHandler(&AnalysisService{analysis: analysis, log: log}, interceptors, medium))
	return withBodyDeadline(mux, cfg, log)
}

// NewControlClient dials the Control API at address (`host:port`) with the gRPC protocol. With
// a certificate it connects over mutual TLS, as edge, and only to the service the CA named
// `control`. Without one it speaks HTTP/2 in plain text, which the Control API serves on
// loopback only.
func NewControlClient(address string, identity mtls.Files) (controlv1connect.ControlServiceClient, error) {
	client, baseURL, err := serviceClient(address, identity, mtls.Control)
	if err != nil {
		return nil, err
	}
	return controlv1connect.NewControlServiceClient(client, baseURL, connect.WithGRPC()), nil
}

// NewAnalysisClient dials the analysis service at address, as NewControlClient dials the
// Control API, and only to the service the CA named `analysis`.
func NewAnalysisClient(address string, identity mtls.Files) (analysisv1connect.AnalysisServiceClient, error) {
	client, baseURL, err := serviceClient(address, identity, mtls.Analysis)
	if err != nil {
		return nil, err
	}
	return analysisv1connect.NewAnalysisServiceClient(client, baseURL, connect.WithGRPC()), nil
}

// serviceClient is an HTTP/2 client for the internal service `server` at address, and the base
// URL to call it at.
func serviceClient(address string, identity mtls.Files, server string) (*http.Client, string, error) {
	var protocols http.Protocols
	transport := &http.Transport{Protocols: &protocols}
	if !identity.Enabled() {
		protocols.SetUnencryptedHTTP2(true)
		return &http.Client{Transport: transport}, "http://" + address, nil
	}
	config, err := identity.Client(server)
	if err != nil {
		return nil, "", err
	}
	protocols.SetHTTP2(true)
	transport.TLSClientConfig = config
	return &http.Client{Transport: transport}, "https://" + address, nil
}

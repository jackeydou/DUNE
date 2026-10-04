package edge

import (
	"log/slog"
	"net/http"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	"github.com/jackeydou/DUNE/go/internal/edge/webui"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
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
	// Everything that is not an API procedure is the console. Its pages need no credentials;
	// the data they ask for does.
	mux.Handle("/", webui.Handler())
	return withBodyDeadline(mux, cfg, log)
}

// NewControlClient dials the Control API at baseURL (`http://host:port`) with the gRPC protocol
// over HTTP/2 without TLS, which is how the orchestrator serves it until services use mTLS.
func NewControlClient(baseURL string) controlv1connect.ControlServiceClient {
	var protocols http.Protocols
	protocols.SetUnencryptedHTTP2(true)
	client := &http.Client{Transport: &http.Transport{Protocols: &protocols}}
	return controlv1connect.NewControlServiceClient(client, baseURL, connect.WithGRPC())
}

// NewAnalysisClient dials the analysis service at baseURL, as NewControlClient dials the
// Control API.
func NewAnalysisClient(baseURL string) analysisv1connect.AnalysisServiceClient {
	var protocols http.Protocols
	protocols.SetUnencryptedHTTP2(true)
	client := &http.Client{Transport: &http.Transport{Protocols: &protocols}}
	return analysisv1connect.NewAnalysisServiceClient(client, baseURL, connect.WithGRPC())
}

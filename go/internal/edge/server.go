package edge

import (
	"log/slog"
	"net/http"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
	"github.com/jackeydou/DUNE/go/internal/mtls"
)

// NewHandler serves the public API. Every procedure but sign-in runs behind the authenticator.
func NewHandler(cfg Config, store *tenant.Store, control controlv1connect.ControlServiceClient, log *slog.Logger) http.Handler {
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
	return withBodyDeadline(mux, cfg, log)
}

// NewControlClient dials the Control API at address (`host:port`) with the gRPC protocol. With
// a certificate it connects over mutual TLS, as edge, and only to the service the CA named
// `control`. Without one it speaks HTTP/2 in plain text, which the Control API serves on
// loopback only.
func NewControlClient(address string, identity mtls.Files) (controlv1connect.ControlServiceClient, error) {
	var protocols http.Protocols
	transport := &http.Transport{Protocols: &protocols}
	scheme := "http"
	if identity.Enabled() {
		config, err := identity.Client(mtls.Control)
		if err != nil {
			return nil, err
		}
		protocols.SetHTTP2(true)
		transport.TLSClientConfig = config
		scheme = "https"
	} else {
		protocols.SetUnencryptedHTTP2(true)
	}
	client := &http.Client{Transport: transport}
	return controlv1connect.NewControlServiceClient(client, scheme+"://"+address, connect.WithGRPC()), nil
}

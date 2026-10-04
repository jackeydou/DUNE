package cli

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
)

// clients are edge's services, all on one endpoint, with the token on every call.
type clients struct {
	endpoint string
	auth     apiv1connect.AuthServiceClient
	users    apiv1connect.UserServiceClient
	runs     apiv1connect.RunServiceClient
	cases    apiv1connect.CaseServiceClient
}

// bearer adds `Authorization: Bearer <token>` to every request, streams included.
type bearer string

func (b bearer) WrapUnary(next connect.UnaryFunc) connect.UnaryFunc {
	return func(ctx context.Context, req connect.AnyRequest) (connect.AnyResponse, error) {
		if b != "" {
			req.Header().Set("Authorization", "Bearer "+string(b))
		}
		return next(ctx, req)
	}
}

func (b bearer) WrapStreamingClient(next connect.StreamingClientFunc) connect.StreamingClientFunc {
	return func(ctx context.Context, spec connect.Spec) connect.StreamingClientConn {
		conn := next(ctx, spec)
		if b != "" {
			conn.RequestHeader().Set("Authorization", "Bearer "+string(b))
		}
		return conn
	}
}

func (b bearer) WrapStreamingHandler(next connect.StreamingHandlerFunc) connect.StreamingHandlerFunc {
	return next
}

// checkEndpoint accepts http(s)://host[:port] with nothing after the host.
func checkEndpoint(raw string) (string, error) {
	if raw == "" {
		return "", errors.New("no edge endpoint: run `swarm login --endpoint https://…`, or set " + envEndpoint)
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || strings.Trim(u.Path, "/") != "" {
		return "", fmt.Errorf("endpoint %q: want http(s)://host[:port], the address of edge", raw)
	}
	return u.Scheme + "://" + u.Host, nil
}

// newClients speaks Connect's protocol, which works over HTTP/1.1 and through any proxy, so the
// CLI needs nothing from the network that a browser does not.
func newClients(cfg Config) (*clients, error) {
	base, err := checkEndpoint(cfg.Endpoint)
	if err != nil {
		return nil, err
	}
	client, err := httpClient(cfg.CAFile)
	if err != nil {
		return nil, err
	}
	opts := connect.WithInterceptors(bearer(cfg.Token))
	return &clients{
		endpoint: base,
		auth:     apiv1connect.NewAuthServiceClient(client, base, opts),
		users:    apiv1connect.NewUserServiceClient(client, base, opts),
		runs:     apiv1connect.NewRunServiceClient(client, base, opts),
		cases:    apiv1connect.NewCaseServiceClient(client, base, opts),
	}, nil
}

// httpClient trusts the system's certificate authorities, and the certificates in caFile when
// one is given: an edge with a self-signed certificate is trusted by naming that certificate,
// never by turning verification off.
func httpClient(caFile string) (*http.Client, error) {
	if caFile == "" {
		return http.DefaultClient, nil
	}
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read the CA file (config `ca_file`, or %s): %w", envCAFile, err)
	}
	pool, err := x509.SystemCertPool()
	if err != nil {
		return nil, fmt.Errorf("load the system's certificate authorities: %w", err)
	}
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("CA file %s holds no PEM certificate; give edge's certificate or the CA that signed it", caFile)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: pool}
	return &http.Client{Transport: transport}, nil
}

// explain turns a failed call into a message for a person: edge's own message, with a hint
// for the failures a person fixes the same way every time.
func explain(op string, err error) error {
	var cerr *connect.Error
	if !errors.As(err, &cerr) {
		return fmt.Errorf("%s: %w", op, err)
	}
	msg := fmt.Sprintf("%s: %s", op, cerr.Message())
	switch cerr.Code() {
	case connect.CodeUnauthenticated:
		msg += " (run `swarm login`)"
	case connect.CodeUnavailable:
		if strings.Contains(cerr.Message(), "dial") {
			msg += " (is edge running at the configured endpoint?)"
		}
	}
	return errors.New(msg)
}

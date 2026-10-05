// Package mtls is how the Go services authenticate each other: every service holds one
// certificate from the deployment's own CA (cmd/swarm-certs), naming it in a URI SAN, and each
// server lists the services allowed to call it. Behavior: docs/architecture.md#service-identity.
package mtls

import (
	"crypto/x509"
	"fmt"
	"net"
	"net/url"
	"strings"
)

// The services of a deployment, as certificates name them.
const (
	Edge         = "edge"
	Control      = "control"
	Worker       = "worker"
	Analysis     = "analysis"
	ModelGateway = "model-gateway"
	Sandboxd     = "sandboxd"
	// Operator is for tools a person runs on the internal network, such as
	// `python -m swarmeval.control.suite` and grpcurl.
	Operator = "operator"
)

const (
	scheme      = "spiffe"
	trustDomain = "swarmeval"
)

// IdentityURI is the URI SAN of service's certificate.
func IdentityURI(service string) *url.URL {
	return &url.URL{Scheme: scheme, Host: trustDomain, Path: "/" + service}
}

// PeerService returns the service a verified certificate names. A certificate from the CA with
// no identity, several, or one of another trust domain names nobody.
func PeerService(cert *x509.Certificate) (string, error) {
	if len(cert.URIs) != 1 {
		return "", fmt.Errorf("certificate %q carries %d URI names, want the one that names its service, %s://%s/<service>", cert.Subject.CommonName, len(cert.URIs), scheme, trustDomain)
	}
	u := cert.URIs[0]
	service := strings.TrimPrefix(u.Path, "/")
	if u.Scheme != scheme || u.Host != trustDomain || service == "" || strings.Contains(service, "/") || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("certificate %q names %q, which is not a service of this deployment (%s://%s/<service>)", cert.Subject.CommonName, u, scheme, trustDomain)
	}
	return service, nil
}

// IsLoopback reports whether a `host:port` listen address is reachable from this machine only.
func IsLoopback(listen string) bool {
	host, _, err := net.SplitHostPort(listen)
	if err != nil {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

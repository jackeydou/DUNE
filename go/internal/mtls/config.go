package mtls

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"slices"
	"strings"
)

// Files are the three PEM files a service is started with: its certificate chain, its private
// key, and the CA that signed every service's certificate.
type Files struct {
	Cert, Key, CA string
}

// FlagSet is what flag.FlagSet and pflag.FlagSet share.
type FlagSet interface {
	StringVar(p *string, name, value, usage string)
}

// Flags registers --mtls-cert, --mtls-key, and --mtls-ca on fs.
func (f *Files) Flags(fs FlagSet) {
	fs.StringVar(&f.Cert, "mtls-cert", "", "this service's certificate from swarm-certs, PEM. With --mtls-key and --mtls-ca")
	fs.StringVar(&f.Key, "mtls-key", "", "private key of --mtls-cert, PEM")
	fs.StringVar(&f.CA, "mtls-ca", "", "the deployment's CA certificate, PEM: only certificates it signed are accepted")
}

// Enabled reports whether the service was given a certificate. Check must have passed.
func (f Files) Enabled() bool {
	return f.Cert != ""
}

// Check refuses a partial set: a service with a certificate but no CA would accept nobody, and
// one with a CA but no certificate could not answer.
func (f Files) Check() error {
	set := 0
	for _, v := range []string{f.Cert, f.Key, f.CA} {
		if v != "" {
			set++
		}
	}
	if set != 0 && set != 3 {
		return fmt.Errorf("--mtls-cert %q, --mtls-key %q, --mtls-ca %q: give all three to use mutual TLS, or none to serve plain text on loopback", f.Cert, f.Key, f.CA)
	}
	return nil
}

func (f Files) load() (tls.Certificate, *x509.CertPool, error) {
	cert, err := tls.LoadX509KeyPair(f.Cert, f.Key)
	if err != nil {
		return tls.Certificate{}, nil, fmt.Errorf("load certificate %s and key %s: %w", f.Cert, f.Key, err)
	}
	pem, err := os.ReadFile(f.CA)
	if err != nil {
		return tls.Certificate{}, nil, fmt.Errorf("read CA certificate: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return tls.Certificate{}, nil, fmt.Errorf("CA file %s holds no PEM certificate; give the ca.crt swarm-certs wrote", f.CA)
	}
	return cert, pool, nil
}

// Server is the TLS configuration of a service that accepts only the listed services. The
// handshake fails for a client with no certificate, one the CA did not sign, or one naming a
// service not in allowed; log gets the reason.
func (f Files) Server(log *slog.Logger, allowed ...string) (*tls.Config, error) {
	cert, pool, err := f.load()
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{cert},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    pool,
		VerifyConnection: func(cs tls.ConnectionState) error {
			service, err := peer(cs)
			if err == nil && !slices.Contains(allowed, service) {
				err = fmt.Errorf("service %q may not call this one; it accepts %s", service, strings.Join(allowed, ", "))
			}
			if err != nil {
				log.Warn("refused a connection", "err", err)
			}
			return err
		},
	}, nil
}

// Client is the TLS configuration for calling `server`: the connection is made only to a
// certificate the CA signed that names that service and the host dialed.
func (f Files) Client(server string) (*tls.Config, error) {
	cert, pool, err := f.load()
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{cert},
		RootCAs:      pool,
		VerifyConnection: func(cs tls.ConnectionState) error {
			service, err := peer(cs)
			if err != nil {
				return err
			}
			if service != server {
				return fmt.Errorf("the server holds the certificate of service %q, want %q: check the address", service, server)
			}
			return nil
		},
	}, nil
}

func peer(cs tls.ConnectionState) (string, error) {
	if len(cs.PeerCertificates) == 0 {
		return "", errors.New("the peer sent no certificate")
	}
	return PeerService(cs.PeerCertificates[0])
}

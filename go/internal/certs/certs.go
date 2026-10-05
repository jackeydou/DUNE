// Package certs issues the certificates services authenticate each other with: one CA per
// deployment and one certificate per service, naming the service in a URI SAN
// (internal/mtls). Behavior: docs/architecture.md#service-identity.
package certs

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"io/fs"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"time"

	"github.com/jackeydou/DUNE/go/internal/mtls"
)

const (
	// LeafValidity is how long a service certificate lasts. Run swarm-certs again before it
	// ends and restart the services.
	LeafValidity = 365 * 24 * time.Hour
	// CAValidity outlasts many leaf rotations, so rotating leaves never changes what services
	// trust.
	CAValidity = 10 * 365 * 24 * time.Hour
	// backdate covers clocks that disagree by a few minutes.
	backdate = 5 * time.Minute
)

// File names under the output directory. Each service's directory holds everything that
// service needs and nothing of another's, so it can be mounted alone.
const (
	CAFile   = "ca.crt"
	CAKey    = "ca.key"
	CertFile = "tls.crt"
	KeyFile  = "tls.key"
)

// Servers are the services others connect to; their certificates also work as server
// certificates for the service's name and loopback. The rest only ever call.
var Servers = []string{mtls.Control, mtls.Analysis, mtls.ModelGateway, mtls.Sandboxd}

// Services is every identity a deployment has.
var Services = []string{mtls.Edge, mtls.Control, mtls.Worker, mtls.Analysis, mtls.ModelGateway, mtls.Sandboxd, mtls.Operator}

type Options struct {
	// Services to issue certificates for. Empty means Services.
	Services []string
	// Hosts adds DNS names or IP addresses a server is reached at, besides its service name,
	// `localhost`, 127.0.0.1, and ::1.
	Hosts map[string][]string
	// NewCA replaces an existing CA. Every service then needs its new certificate before any
	// two of them can talk.
	NewCA bool
	Now   time.Time
}

// Generate writes `ca.crt`, `ca.key`, and `<service>/{ca.crt,tls.crt,tls.key}` under dir. An
// existing CA is kept and signs the new certificates, so services restarted one at a time keep
// trusting each other; existing service certificates are replaced.
func Generate(dir string, opts Options) error {
	services := opts.Services
	if len(services) == 0 {
		services = Services
	}
	for _, s := range services {
		if !slices.Contains(Services, s) {
			return fmt.Errorf("unknown service %q: the services are %v", s, Services)
		}
	}
	for s := range opts.Hosts {
		if !slices.Contains(Servers, s) {
			return fmt.Errorf("host names for %q: only %v accept connections", s, Servers)
		}
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("create %s: %w", dir, err)
	}
	ca, caKey, caPEM, err := loadOrCreateCA(dir, opts)
	if err != nil {
		return err
	}
	for _, service := range services {
		if err := issue(dir, service, opts, ca, caKey, caPEM); err != nil {
			return fmt.Errorf("certificate for %s: %w", service, err)
		}
	}
	return nil
}

func loadOrCreateCA(dir string, opts Options) (*x509.Certificate, *ecdsa.PrivateKey, []byte, error) {
	certPath, keyPath := filepath.Join(dir, CAFile), filepath.Join(dir, CAKey)
	certPEM, certErr := os.ReadFile(certPath)
	keyPEM, keyErr := os.ReadFile(keyPath)
	missing := errors.Is(certErr, fs.ErrNotExist) && errors.Is(keyErr, fs.ErrNotExist)
	if opts.NewCA || missing {
		return createCA(certPath, keyPath, opts.Now)
	}
	if err := errors.Join(certErr, keyErr); err != nil {
		return nil, nil, nil, fmt.Errorf("read the CA in %s: %w. Restore the missing file, or pass --new-ca to replace the CA and every certificate", dir, err)
	}
	certBlock, _ := pem.Decode(certPEM)
	keyBlock, _ := pem.Decode(keyPEM)
	if certBlock == nil || keyBlock == nil {
		return nil, nil, nil, fmt.Errorf("%s or %s is not PEM. Pass --new-ca to replace the CA and every certificate", certPath, keyPath)
	}
	ca, err := x509.ParseCertificate(certBlock.Bytes)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse %s: %w", certPath, err)
	}
	parsed, err := x509.ParsePKCS8PrivateKey(keyBlock.Bytes)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("parse %s: %w", keyPath, err)
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	if !ok || !key.PublicKey.Equal(ca.PublicKey) {
		return nil, nil, nil, fmt.Errorf("%s is not the key of %s. Pass --new-ca to replace the CA and every certificate", keyPath, certPath)
	}
	if ca.NotAfter.Before(opts.Now.Add(LeafValidity)) {
		return nil, nil, nil, fmt.Errorf("the CA in %s ends %s, before a certificate issued now would. Pass --new-ca to replace it, then restart every service", certPath, ca.NotAfter.Format(time.DateOnly))
	}
	return ca, key, certPEM, nil
}

func createCA(certPath, keyPath string, now time.Time) (*x509.Certificate, *ecdsa.PrivateKey, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	template := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: "swarmeval service CA"},
		NotBefore:             now.Add(-backdate),
		NotAfter:              now.Add(CAValidity),
		IsCA:                  true,
		BasicConstraintsValid: true,
		MaxPathLenZero:        true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("sign the CA certificate: %w", err)
	}
	ca, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, nil, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	if err := writeKey(keyPath, key); err != nil {
		return nil, nil, nil, err
	}
	if err := write(certPath, certPEM, 0o644); err != nil {
		return nil, nil, nil, err
	}
	return ca, key, certPEM, nil
}

func issue(dir, service string, opts Options, ca *x509.Certificate, caKey *ecdsa.PrivateKey, caPEM []byte) error {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	template := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: service},
		NotBefore:    opts.Now.Add(-backdate),
		NotAfter:     opts.Now.Add(LeafValidity),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		URIs:         []*url.URL{mtls.IdentityURI(service)},
	}
	if slices.Contains(Servers, service) {
		template.ExtKeyUsage = append(template.ExtKeyUsage, x509.ExtKeyUsageServerAuth)
		template.DNSNames = []string{service, "localhost"}
		template.IPAddresses = []net.IP{net.IPv4(127, 0, 0, 1), net.IPv6loopback}
		for _, host := range opts.Hosts[service] {
			if ip := net.ParseIP(host); ip != nil {
				template.IPAddresses = append(template.IPAddresses, ip)
			} else {
				template.DNSNames = append(template.DNSNames, host)
			}
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca, &key.PublicKey, caKey)
	if err != nil {
		return fmt.Errorf("sign: %w", err)
	}
	// The bundle is built beside the service's directory and swapped in whole, so a service
	// starting during a rotation reads the old bundle or the new one, or for an instant finds
	// none and fails to start; it never reads a key, certificate, and CA of different runs.
	out := filepath.Join(dir, service)
	next, old := out+".new", out+".old"
	for _, leftover := range []string{next, old} {
		if err := os.RemoveAll(leftover); err != nil {
			return err
		}
	}
	if err := os.Mkdir(next, 0o755); err != nil {
		return err
	}
	if err := writeKey(filepath.Join(next, KeyFile), key); err != nil {
		return err
	}
	if err := write(filepath.Join(next, CertFile), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil {
		return err
	}
	if err := write(filepath.Join(next, CAFile), caPEM, 0o644); err != nil {
		return err
	}
	if err := os.Rename(out, old); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.Rename(next, out); err != nil {
		return err
	}
	return os.RemoveAll(old)
}

func serial() *big.Int {
	// crypto/rand.Int fails only when the system's random source does, which Go treats as
	// fatal itself.
	n, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		panic(err)
	}
	return n
}

func writeKey(path string, key *ecdsa.PrivateKey) error {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	return write(path, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600)
}

// write replaces path in one rename, so no reader sees part of a file.
func write(path string, data []byte, mode fs.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(tmp.Name()) }() // gone already after a successful rename
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write %s: %w", path, err)
	}
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return os.Rename(tmp.Name(), path)
}

// Paths are the files Generate wrote for service under dir.
func Paths(dir, service string) mtls.Files {
	d := filepath.Join(dir, service)
	return mtls.Files{Cert: filepath.Join(d, CertFile), Key: filepath.Join(d, KeyFile), CA: filepath.Join(d, CAFile)}
}

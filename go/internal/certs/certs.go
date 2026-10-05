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
	// LeafValidity is how long a certificate lasts.
	LeafValidity = 365 * 24 * time.Hour
	// RenewBefore is how long before its end a certificate is replaced by the next run. Until
	// then a run leaves it alone, so running at every start of a deployment changes nothing.
	RenewBefore = 30 * 24 * time.Hour
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
	// Renew replaces every service certificate, also those with time left.
	Renew bool
	Now   time.Time
}

// Generate writes `ca.crt`, `ca.key`, and `<service>/{ca.crt,tls.crt,tls.key}` under dir, and
// returns the services whose certificate it wrote. An existing CA is kept and signs the new
// certificates, so services restarted one at a time keep trusting each other. A service
// certificate is kept while the CA signed it, it names the same hosts, and it has more than
// RenewBefore left.
func Generate(dir string, opts Options) ([]string, error) {
	services := opts.Services
	if len(services) == 0 {
		services = Services
	}
	for _, s := range services {
		if !slices.Contains(Services, s) {
			return nil, fmt.Errorf("unknown service %q: the services are %v", s, Services)
		}
	}
	for s := range opts.Hosts {
		if !slices.Contains(Servers, s) {
			return nil, fmt.Errorf("host names for %q: only %v accept connections", s, Servers)
		}
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create %s: %w", dir, err)
	}
	ca, caKey, caPEM, err := loadOrCreateCA(dir, opts)
	if err != nil {
		return nil, err
	}
	var written []string
	for _, service := range services {
		template := leaf(service, opts)
		if !opts.Renew && current(dir, service, template, ca, opts.Now) {
			continue
		}
		if err := issue(dir, service, template, ca, caKey, caPEM); err != nil {
			return nil, fmt.Errorf("certificate for %s: %w", service, err)
		}
		written = append(written, service)
	}
	return written, nil
}

// current reports whether service's files under dir can stay: a certificate the CA signed,
// with the names template would give it, its own key beside it, and more than RenewBefore left.
func current(dir, service string, template, ca *x509.Certificate, now time.Time) bool {
	files := Paths(dir, service)
	cert, err := readCertificate(files.Cert)
	if err != nil {
		return false
	}
	if !keyMatches(files.Key, cert) {
		return false
	}
	if trusted, err := readCertificate(files.CA); err != nil || !trusted.Equal(ca) {
		return false
	}
	return cert.CheckSignatureFrom(ca) == nil &&
		slices.Equal(cert.DNSNames, template.DNSNames) &&
		slices.EqualFunc(cert.IPAddresses, template.IPAddresses, net.IP.Equal) &&
		cert.NotAfter.After(now.Add(RenewBefore))
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

// leaf is the certificate service gets, before signing.
func leaf(service string, opts Options) *x509.Certificate {
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
	return template
}

func issue(dir, service string, template, ca *x509.Certificate, caKey *ecdsa.PrivateKey, caPEM []byte) error {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca, &key.PublicKey, caKey)
	if err != nil {
		return fmt.Errorf("sign: %w", err)
	}
	return replaceDir(filepath.Join(dir, service), key, der, caPEM)
}

// replaceDir makes out hold a key, its certificate, and, when given, the CA, all of this run.
// The directory is built beside out and swapped in whole, so a service starting during a
// rotation reads the old directory or the new one, or for an instant finds none and fails to
// start; it never reads files of different runs.
func replaceDir(out string, key *ecdsa.PrivateKey, der, caPEM []byte) error {
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
	if caPEM != nil {
		if err := write(filepath.Join(next, CAFile), caPEM, 0o644); err != nil {
			return err
		}
	}
	if err := os.Rename(out, old); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.Rename(next, out); err != nil {
		return err
	}
	return os.RemoveAll(old)
}

// keyMatches reports whether the PEM key at path is the private key of cert. A run cut short,
// or a damaged file, can leave a key that is not: such a pair is replaced, not kept.
func keyMatches(path string, cert *x509.Certificate) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return false
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return false
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	return ok && key.PublicKey.Equal(cert.PublicKey)
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

// PublicDir is where Public writes, under the output directory.
const PublicDir = "public"

// Public writes `public/{tls.crt,tls.key}` under dir: a self-signed certificate for the
// address people reach edge at, for a deployment that has no certificate from a public CA.
// It is not signed by the service CA, so trusting it trusts nothing else. People pin it in
// their browser and CLI, so a certificate already there is kept while it names the same hosts
// and has more than RenewBefore left. It reports whether it wrote a new one.
func Public(dir string, hosts []string, now time.Time) (bool, error) {
	if len(hosts) == 0 {
		return false, errors.New("no host for the public certificate: give the name or address people reach edge at")
	}
	out := filepath.Join(dir, PublicDir)
	certPath, keyPath := filepath.Join(out, CertFile), filepath.Join(out, KeyFile)
	template := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: hosts[0]},
		NotBefore:             now.Add(-backdate),
		NotAfter:              now.Add(LeafValidity),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
	}
	for _, host := range hosts {
		if ip := net.ParseIP(host); ip != nil {
			template.IPAddresses = append(template.IPAddresses, ip)
		} else {
			template.DNSNames = append(template.DNSNames, host)
		}
	}
	if current, err := readCertificate(certPath); err == nil {
		sameIPs := slices.EqualFunc(current.IPAddresses, template.IPAddresses, net.IP.Equal)
		if keyMatches(keyPath, current) && sameIPs && slices.Equal(current.DNSNames, template.DNSNames) && current.NotAfter.After(now.Add(RenewBefore)) {
			return false, nil
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return false, fmt.Errorf("read the public certificate: %w. Delete %s to have it replaced", err, out)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return false, err
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return false, fmt.Errorf("sign the public certificate: %w", err)
	}
	return true, replaceDir(out, key, der, nil)
}

func readCertificate(path string) (*x509.Certificate, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, fmt.Errorf("%s is not PEM", path)
	}
	return x509.ParseCertificate(block.Bytes)
}

// Paths are the files Generate wrote for service under dir.
func Paths(dir, service string) mtls.Files {
	d := filepath.Join(dir, service)
	return mtls.Files{Cert: filepath.Join(d, CertFile), Key: filepath.Join(d, KeyFile), CA: filepath.Join(d, CAFile)}
}

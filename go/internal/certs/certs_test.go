package certs

import (
	"crypto/x509"
	"encoding/pem"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackeydou/DUNE/go/internal/mtls"
)

func readCert(t *testing.T, path string) *x509.Certificate {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(data)
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func verify(ca, leaf *x509.Certificate, usage x509.ExtKeyUsage, at time.Time) error {
	pool := x509.NewCertPool()
	pool.AddCert(ca)
	_, err := leaf.Verify(x509.VerifyOptions{Roots: pool, KeyUsages: []x509.ExtKeyUsage{usage}, CurrentTime: at})
	return err
}

func TestGenerateIssuesOneIdentityPerService(t *testing.T) {
	dir, now := t.TempDir(), time.Now()
	written, err := Generate(dir, Options{Now: now, Hosts: map[string][]string{mtls.Control: {"control.internal", "10.0.0.7"}}})
	if err != nil || !slices.Equal(written, Services) {
		t.Fatalf("wrote %v, %v", written, err)
	}
	ca := readCert(t, filepath.Join(dir, CAFile))
	for _, service := range Services {
		files := Paths(dir, service)
		leaf := readCert(t, files.Cert)
		if got, err := mtls.PeerService(leaf); err != nil || got != service {
			t.Errorf("%s: certificate names %q, %v", service, got, err)
		}
		if err := verify(ca, leaf, x509.ExtKeyUsageClientAuth, now); err != nil {
			t.Errorf("%s: not a client certificate of the CA: %v", service, err)
		}
		serves := verify(ca, leaf, x509.ExtKeyUsageServerAuth, now) == nil
		if want := slices.Contains(Servers, service); serves != want {
			t.Errorf("%s: usable as a server certificate = %v, want %v", service, serves, want)
		}
		if !leaf.NotAfter.Equal(now.Add(LeafValidity).Truncate(time.Second)) {
			t.Errorf("%s: valid until %s", service, leaf.NotAfter)
		}
		if own, shared := readCert(t, files.CA), ca; !own.Equal(shared) {
			t.Errorf("%s: its ca.crt is not the CA", service)
		}
		info, err := os.Stat(files.Key)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Errorf("%s: key mode %v, %v", service, info.Mode(), err)
		}
	}
	control := readCert(t, Paths(dir, mtls.Control).Cert)
	if !slices.Equal(control.DNSNames, []string{"control", "localhost", "control.internal"}) || len(control.IPAddresses) != 3 {
		t.Errorf("control: DNS %v, IPs %v", control.DNSNames, control.IPAddresses)
	}
	if worker := readCert(t, Paths(dir, mtls.Worker).Cert); len(worker.DNSNames) != 0 || len(worker.IPAddresses) != 0 {
		t.Errorf("worker only calls, yet its certificate names hosts: %v %v", worker.DNSNames, worker.IPAddresses)
	}
	if info, err := os.Stat(filepath.Join(dir, CAKey)); err != nil || info.Mode().Perm() != 0o600 {
		t.Errorf("CA key mode %v, %v", info.Mode(), err)
	}
}

func TestRunningAgainKeepsWhatHasTimeLeft(t *testing.T) {
	dir, now := t.TempDir(), time.Now()
	generate := func(opts Options) []string {
		t.Helper()
		written, err := Generate(dir, opts)
		if err != nil {
			t.Fatal(err)
		}
		return written
	}
	generate(Options{Now: now})
	ca, old := readCert(t, filepath.Join(dir, CAFile)), readCert(t, Paths(dir, mtls.Worker).Cert)

	// A deployment runs it at every start: nothing changes under running services.
	if written := generate(Options{Now: now.Add(300 * 24 * time.Hour)}); written != nil {
		t.Fatalf("with 65 days left it rewrote %v", written)
	}
	if !readCert(t, Paths(dir, mtls.Worker).Cert).Equal(old) {
		t.Fatal("worker's certificate changed")
	}

	// Other hosts, a lost key, and a forced renewal each replace one certificate.
	hosts := map[string][]string{mtls.Control: {"control.internal"}}
	if written := generate(Options{Now: now, Hosts: hosts}); !slices.Equal(written, []string{mtls.Control}) {
		t.Fatalf("with a new host for control it wrote %v", written)
	}
	if err := os.Remove(Paths(dir, mtls.Sandboxd).Key); err != nil {
		t.Fatal(err)
	}
	if written := generate(Options{Now: now, Hosts: hosts}); !slices.Equal(written, []string{mtls.Sandboxd}) {
		t.Fatalf("with sandboxd's key gone it wrote %v", written)
	}
	if written := generate(Options{Now: now, Hosts: hosts, Renew: true, Services: []string{mtls.Edge}}); !slices.Equal(written, []string{mtls.Edge}) {
		t.Fatalf("--renew for edge wrote %v", written)
	}

	// Within 30 days of the end, every certificate is renewed under the same CA.
	later := now.Add(340 * 24 * time.Hour)
	if written := generate(Options{Now: later, Hosts: hosts}); !slices.Equal(written, Services) {
		t.Fatalf("with 25 days left it wrote %v", written)
	}
	if !readCert(t, filepath.Join(dir, CAFile)).Equal(ca) {
		t.Fatal("the CA was replaced")
	}
	renewed := readCert(t, Paths(dir, mtls.Worker).Cert)
	if renewed.Equal(old) || verify(ca, renewed, x509.ExtKeyUsageClientAuth, later.Add(LeafValidity-time.Hour)) != nil {
		t.Fatalf("worker's certificate was not renewed under the same CA: until %s", renewed.NotAfter)
	}

	// A new CA replaces every certificate, whatever time they had left.
	if written := generate(Options{Now: later, Hosts: hosts, NewCA: true}); !slices.Equal(written, Services) {
		t.Fatalf("--new-ca wrote %v", written)
	}
	if replaced := readCert(t, filepath.Join(dir, CAFile)); replaced.Equal(ca) {
		t.Fatal("--new-ca kept the CA")
	} else if err := verify(replaced, readCert(t, Paths(dir, mtls.Sandboxd).Cert), x509.ExtKeyUsageServerAuth, later); err != nil {
		t.Fatalf("after --new-ca: %v", err)
	}
}

func TestGenerateRefuses(t *testing.T) {
	now := time.Now()
	for name, tc := range map[string]struct {
		prepare func(t *testing.T, dir string)
		opts    Options
		want    string
	}{
		"an unknown service": {opts: Options{Services: []string{"net-gateway"}}, want: `unknown service "net-gateway"`},
		"hosts for a service nobody connects to": {
			opts: Options{Hosts: map[string][]string{mtls.Worker: {"w"}}}, want: `host names for "worker"`,
		},
		"a CA without its key": {
			prepare: func(t *testing.T, dir string) {
				if _, err := Generate(dir, Options{Now: now}); err != nil {
					t.Fatal(err)
				}
				if err := os.Remove(filepath.Join(dir, CAKey)); err != nil {
					t.Fatal(err)
				}
			},
			want: "--new-ca",
		},
		"a CA that ends before the certificates would": {
			prepare: func(t *testing.T, dir string) {
				if _, err := Generate(dir, Options{Now: now.Add(-CAValidity + 24*time.Hour)}); err != nil {
					t.Fatal(err)
				}
			},
			want: "Pass --new-ca to replace it",
		},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if tc.prepare != nil {
				tc.prepare(t, dir)
			}
			tc.opts.Now = now
			_, err := Generate(dir, tc.opts)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want an error containing %q", err, tc.want)
			}
		})
	}
}

func TestPublicCertificateIsSelfSignedAndKeptWhileItFits(t *testing.T) {
	dir, now := t.TempDir(), time.Now()
	hosts := []string{"swarm.example.com", "localhost", "192.0.2.10"}
	if wrote, err := Public(dir, hosts, now); err != nil || !wrote {
		t.Fatalf("first run: wrote %v, %v", wrote, err)
	}
	path := filepath.Join(dir, PublicDir, CertFile)
	first := readCert(t, path)
	if err := verify(first, first, x509.ExtKeyUsageServerAuth, now); err != nil {
		t.Fatalf("not a server certificate that verifies against itself: %v", err)
	}
	if err := first.VerifyHostname("swarm.example.com"); err != nil {
		t.Error(err)
	}
	if err := first.VerifyHostname("192.0.2.10"); err != nil {
		t.Error(err)
	}
	if first.IsCA || len(first.URIs) != 0 {
		t.Errorf("the public certificate could sign others or names a service: CA %v, URIs %v", first.IsCA, first.URIs)
	}

	// People have pinned it: the same hosts with time left keep it.
	if wrote, err := Public(dir, hosts, now.Add(300*24*time.Hour)); err != nil || wrote {
		t.Fatalf("with 65 days left: wrote %v, %v", wrote, err)
	}
	if wrote, err := Public(dir, hosts, now.Add(340*24*time.Hour)); err != nil || !wrote {
		t.Fatalf("with 25 days left: wrote %v, %v", wrote, err)
	}
	if wrote, err := Public(dir, []string{"other.example.com"}, now.Add(340*24*time.Hour)); err != nil || !wrote {
		t.Fatalf("with other hosts: wrote %v, %v", wrote, err)
	}
	if got := readCert(t, path).DNSNames; !slices.Equal(got, []string{"other.example.com"}) {
		t.Errorf("names %v", got)
	}
	if _, err := Public(dir, nil, now); err == nil {
		t.Error("a public certificate for no host was accepted")
	}
}

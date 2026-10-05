package certs

import (
	"crypto/tls"
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
	if err := Generate(dir, Options{Now: now, Hosts: map[string][]string{mtls.Control: {"control.internal", "10.0.0.7"}}}); err != nil {
		t.Fatal(err)
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

func TestRunningAgainKeepsTheCAAndReplacesCertificates(t *testing.T) {
	dir, now := t.TempDir(), time.Now()
	if err := Generate(dir, Options{Now: now}); err != nil {
		t.Fatal(err)
	}
	ca, old := readCert(t, filepath.Join(dir, CAFile)), readCert(t, Paths(dir, mtls.Worker).Cert)
	later := now.Add(300 * 24 * time.Hour)
	if err := Generate(dir, Options{Now: later, Services: []string{mtls.Worker}}); err != nil {
		t.Fatal(err)
	}
	if !readCert(t, filepath.Join(dir, CAFile)).Equal(ca) {
		t.Fatal("the CA was replaced")
	}
	renewed := readCert(t, Paths(dir, mtls.Worker).Cert)
	if renewed.Equal(old) || verify(ca, renewed, x509.ExtKeyUsageClientAuth, later.Add(LeafValidity-time.Hour)) != nil {
		t.Fatalf("worker's certificate was not renewed under the same CA: until %s", renewed.NotAfter)
	}

	if err := Generate(dir, Options{Now: later, NewCA: true}); err != nil {
		t.Fatal(err)
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
				if err := Generate(dir, Options{Now: now}); err != nil {
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
				if err := Generate(dir, Options{Now: now.Add(-CAValidity + 24*time.Hour)}); err != nil {
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
			err := Generate(dir, tc.opts)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want an error containing %q", err, tc.want)
			}
		})
	}
}

func TestABundleIsReplacedWholeAndLeftoversOfAnInterruptedRunAreCleared(t *testing.T) {
	dir, now := t.TempDir(), time.Now()
	if err := Generate(dir, Options{Now: now}); err != nil {
		t.Fatal(err)
	}
	// A run that died between its two renames leaves the old bundle aside and none in place;
	// one that died earlier leaves a half-written new one.
	worker := filepath.Join(dir, mtls.Worker)
	if err := os.Rename(worker, worker+".old"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(worker+".new", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Generate(dir, Options{Now: now}); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".new") || strings.HasSuffix(e.Name(), ".old") {
			t.Errorf("left %s behind", e.Name())
		}
	}
	// Every file of a bundle comes from one run: the key is the certificate's, under this CA.
	files := Paths(dir, mtls.Worker)
	if _, err := tls.LoadX509KeyPair(files.Cert, files.Key); err != nil {
		t.Fatalf("worker's key and certificate do not belong together: %v", err)
	}
	if err := verify(readCert(t, files.CA), readCert(t, files.Cert), x509.ExtKeyUsageClientAuth, now); err != nil {
		t.Fatal(err)
	}
}

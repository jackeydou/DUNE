package cli

import (
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var timeZero = time.Unix(0, 0)

func TestConfigIsPrivateAndTheEnvironmentWins(t *testing.T) {
	path := filepath.Join(t.TempDir(), "swarm", "config.yaml")
	t.Setenv(envConfig, path)
	t.Setenv(envEndpoint, "")
	t.Setenv(envToken, "")
	t.Setenv(envCAFile, "")
	if err := saveConfig(path, Config{Endpoint: "https://swarm.example.com", Token: "swm_a", TokenID: "tok_1"}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("config file mode %v, want 0600", info.Mode().Perm())
	}
	cfg, _, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg != (Config{Endpoint: "https://swarm.example.com", Token: "swm_a", TokenID: "tok_1"}) {
		t.Fatalf("read back %+v", cfg)
	}

	t.Setenv(envEndpoint, "http://127.0.0.1:7443")
	t.Setenv(envToken, "swm_b")
	cfg, _, err = loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg != (Config{Endpoint: "http://127.0.0.1:7443", Token: "swm_b"}) {
		t.Fatalf("with the environment set: %+v", cfg)
	}
}

func TestNoConfigFileIsAnEmptyConfig(t *testing.T) {
	t.Setenv(envConfig, filepath.Join(t.TempDir(), "none.yaml"))
	t.Setenv(envEndpoint, "")
	t.Setenv(envToken, "")
	t.Setenv(envCAFile, "")
	cfg, _, err := loadConfig()
	if err != nil || cfg != (Config{}) {
		t.Fatalf("got %+v, %v", cfg, err)
	}
}

func TestEndpoints(t *testing.T) {
	for raw, want := range map[string]string{
		"https://swarm.example.com":  "https://swarm.example.com",
		"https://swarm.example.com/": "https://swarm.example.com",
		"http://127.0.0.1:7443":      "http://127.0.0.1:7443",
	} {
		if got, err := checkEndpoint(raw); err != nil || got != want {
			t.Errorf("%q: got %q, %v", raw, got, err)
		}
	}
	for _, bad := range []string{"", "swarm.example.com", "https://x/api", "ftp://x"} {
		if _, err := checkEndpoint(bad); err == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}

func TestCAFileIsTrustedBesidesTheSystemRoots(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)
	caFile := filepath.Join(t.TempDir(), "edge.crt")
	cert := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := os.WriteFile(caFile, cert, 0o644); err != nil {
		t.Fatal(err)
	}

	client, err := httpClient(caFile)
	if err != nil {
		t.Fatal(err)
	}
	res, err := client.Get(srv.URL)
	if err != nil {
		t.Fatalf("a server whose certificate is in the CA file was refused: %v", err)
	}
	_ = res.Body.Close()

	plain, err := httpClient("")
	if err != nil {
		t.Fatal(err)
	}
	if res, err := plain.Get(srv.URL); err == nil {
		_ = res.Body.Close()
		t.Fatal("a self-signed server was trusted without a CA file")
	}

	if _, err := httpClient(filepath.Join(t.TempDir(), "missing.crt")); err == nil {
		t.Error("a missing CA file was accepted")
	}
	empty := filepath.Join(t.TempDir(), "empty.crt")
	if err := os.WriteFile(empty, []byte("not a certificate"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := httpClient(empty); err == nil || !strings.Contains(err.Error(), "holds no PEM certificate") {
		t.Errorf("a CA file without certificates: %v", err)
	}

	t.Setenv(envConfig, filepath.Join(t.TempDir(), "none.yaml"))
	t.Setenv(envCAFile, caFile)
	if cfg, _, err := loadConfig(); err != nil || cfg.CAFile != caFile {
		t.Errorf("%s was not applied: %+v, %v", envCAFile, cfg, err)
	}
}

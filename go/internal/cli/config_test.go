package cli

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

var timeZero = time.Unix(0, 0)

func TestConfigIsPrivateAndTheEnvironmentWins(t *testing.T) {
	path := filepath.Join(t.TempDir(), "swarm", "config.yaml")
	t.Setenv(envConfig, path)
	t.Setenv(envEndpoint, "")
	t.Setenv(envToken, "")
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

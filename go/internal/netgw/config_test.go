package netgw

import (
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const validConfig = `{
  "run_id": "readonly_web_deaddrop.ab12cd.v0.e01",
  "gateway": "10.231.0.1",
  "sandboxes": [
    {"sandbox_id": "agent_a", "subnet": "10.231.0.0/28"},
    {"sandbox_id": "agent_b", "subnet": "10.231.0.16/28"}
  ],
  "resolvers": ["1.1.1.1:53"],
  "policy": {
    "default_egress": "deny",
    "rules": [
      {"id": "gh", "host": "*.github.com", "action": "allow"},
      {"id": "txt", "protocol": "dns", "qtype": "TXT", "action": "log_and_deny"},
      {"id": "ssh", "cidr": "10.8.0.0/16", "port": 22, "action": "allow"}
    ]
  },
  "platform": {
    "listen": "0.0.0.0:7443",
    "cert_file": "/cfg/server.crt",
    "key_file": "/cfg/server.key",
    "client_ca_file": "/cfg/client-ca.crt"
  }
}`

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadValid(t *testing.T) {
	cfg, err := Load(writeConfig(t, validConfig))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Gateway != netip.MustParseAddr("10.231.0.1") {
		t.Errorf("gateway = %v", cfg.Gateway)
	}
	id, ok := cfg.Sandbox(netip.MustParseAddr("10.231.0.20"))
	if !ok || id != "agent_b" {
		t.Errorf("Sandbox(10.231.0.20) = %q, %v; want agent_b, true", id, ok)
	}
	if _, ok := cfg.Sandbox(netip.MustParseAddr("10.231.9.9")); ok {
		t.Errorf("Sandbox of an unlisted address should not resolve")
	}
	pol, err := cfg.CompilePolicy()
	if err != nil {
		t.Fatalf("CompilePolicy: %v", err)
	}
	if act, rule := pol.Decide(Request{Protocol: "tcp", Host: "x.github.com"}); act != ActionAllow || rule != "gh" {
		t.Errorf("github decision = %q, %q", act, rule)
	}
}

func TestLoadRejects(t *testing.T) {
	bad := []struct {
		name string
		body string
		want string
	}{
		{
			"unknown field",
			strings.Replace(validConfig, `"resolvers":`, `"resolver":`, 1),
			"unknown field",
		},
		{
			"default egress allow",
			strings.Replace(validConfig, `"default_egress": "deny"`, `"default_egress": "allow"`, 1),
			"must be `deny`",
		},
		{
			"duplicate rule id",
			strings.Replace(validConfig, `"id": "txt"`, `"id": "gh"`, 1),
			"used twice",
		},
		{
			"host and cidr together",
			strings.Replace(validConfig, `{"id": "ssh", "cidr": "10.8.0.0/16", "port": 22, "action": "allow"}`,
				`{"id": "ssh", "cidr": "10.8.0.0/16", "host": "x.test", "action": "allow"}`, 1),
			"not both",
		},
		{
			"subnet host bits set",
			strings.Replace(validConfig, `"10.231.0.16/28"`, `"10.231.0.17/28"`, 1),
			"host bits set",
		},
		{
			"unknown action",
			strings.Replace(validConfig, `"action": "log_and_deny"`, `"action": "route"`, 1),
			"not one of allow",
		},
	}
	for _, c := range bad {
		t.Run(c.name, func(t *testing.T) {
			_, err := Load(writeConfig(t, c.body))
			if err == nil {
				t.Fatalf("Load should have failed")
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Fatalf("error %q does not contain %q", err, c.want)
			}
		})
	}
}

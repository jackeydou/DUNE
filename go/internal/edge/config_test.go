package edge

import (
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestParsePublicURL(t *testing.T) {
	for _, ok := range []string{"https://swarm.example.com", "http://127.0.0.1:7443", "https://swarm.example.com/"} {
		if _, err := ParsePublicURL(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"", "swarm.example.com", "ftp://x", "https://", "https://x/console", "https://x?a=1", "https://u:p@x"} {
		if _, err := ParsePublicURL(bad); err == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}

func TestCookieFollowsThePublicScheme(t *testing.T) {
	expires := time.Now().Add(time.Hour)
	secure, _ := ParsePublicURL("https://swarm.example.com:8443")
	c := Config{PublicURL: secure}.sessionCookie("v", expires)
	if c.Name != "__Host-swarm_session" || !c.Secure || !c.HttpOnly || c.SameSite != http.SameSiteStrictMode || c.Path != "/" || c.Domain != "" {
		t.Fatalf("https cookie: %+v", c)
	}
	if got := (Config{PublicURL: secure}).origin(); got != "https://swarm.example.com:8443" {
		t.Fatalf("origin %q", got)
	}
	plain, _ := ParsePublicURL("http://127.0.0.1:7443")
	c = Config{PublicURL: plain}.sessionCookie("v", expires)
	if c.Name != "swarm_session" || c.Secure || !c.HttpOnly {
		t.Fatalf("http cookie: %+v", c)
	}
	if cleared := (Config{PublicURL: plain}).clearedCookie().String(); !strings.Contains(cleared, "Max-Age=0") {
		t.Fatalf("cleared cookie %q does not expire at once", cleared)
	}
}

package netgw

import (
	"net/netip"
	"testing"
)

func TestPolicyDecideFirstHit(t *testing.T) {
	p := &Policy{
		DefaultEgress: ActionDeny,
		Rules: []Rule{
			{ID: "gh", Match: Match{Host: "*.github.com"}, Action: ActionAllow},
			{ID: "txt", Match: Match{Protocol: "dns", QType: "TXT"}, Action: ActionLogAndDeny},
			{ID: "mkt", Match: Match{Host: "api.market.internal"}, Action: ActionAllow},
			{ID: "ip", Match: Match{Prefix: netip.MustParsePrefix("10.8.0.0/16"), Port: 22}, Action: ActionAllow},
		},
	}
	cases := []struct {
		name     string
		req      Request
		wantAct  Action
		wantRule string
	}{
		{"subdomain allowed", Request{Protocol: "tcp", Host: "api.github.com", DstPort: 443}, ActionAllow, "gh"},
		{"apex not caught by wildcard", Request{Protocol: "tcp", Host: "github.com", DstPort: 443}, ActionDeny, ""},
		{"exact host", Request{Protocol: "tcp", Host: "api.market.internal"}, ActionAllow, "mkt"},
		{"dns txt log_and_deny", Request{Protocol: "dns", Host: "x.evil.test", QType: "TXT"}, ActionLogAndDeny, "txt"},
		{"dns a falls through", Request{Protocol: "dns", Host: "x.evil.test", QType: "A"}, ActionDeny, ""},
		{"ip and port", Request{Protocol: "tcp", DstIP: netip.MustParseAddr("10.8.1.2"), DstPort: 22}, ActionAllow, "ip"},
		{"ip wrong port", Request{Protocol: "tcp", DstIP: netip.MustParseAddr("10.8.1.2"), DstPort: 80}, ActionDeny, ""},
		{"host rule ignores nameless connection", Request{Protocol: "tcp", DstIP: netip.MustParseAddr("140.82.1.1"), DstPort: 443}, ActionDeny, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			act, rule := p.Decide(c.req)
			if act != c.wantAct || rule != c.wantRule {
				t.Fatalf("Decide(%+v) = %q, %q; want %q, %q", c.req, act, rule, c.wantAct, c.wantRule)
			}
		})
	}
}

func TestMatchHost(t *testing.T) {
	cases := []struct {
		pattern, host string
		want          bool
	}{
		{"api.github.com", "api.github.com", true},
		{"api.github.com", "API.GitHub.Com", true},
		{"api.github.com", "api.github.com.", true},
		{"*.github.com", "api.github.com", true},
		{"*.github.com", "a.b.github.com", true},
		{"*.github.com", "github.com", false},
		{"*.github.com", ".github.com", false},
		{"*.github.com", "evilgithub.com", false},
		{"api.github.com", "", false},
	}
	for _, c := range cases {
		if got := matchHost(c.pattern, c.host); got != c.want {
			t.Errorf("matchHost(%q, %q) = %v; want %v", c.pattern, c.host, got, c.want)
		}
	}
}

// Package netgw is the per-run network gateway: it is the only neighbor and default route of
// every sandbox in its run, applies the run's policy to all traffic, answers DNS, and reports
// every connection, datagram flow, and DNS query to the run's worker. Design:
// docs/services/net-gateway.md and runtime spec decision 24.
//
// This file holds the policy engine, which is pure and platform-independent. The Linux capture
// and forwarding path is in capture_linux.go; the worker event stream is in stream.go.
package netgw

import (
	"net/netip"
	"strings"
)

// Action is what a rule, or the default, does with matching traffic.
type Action string

const (
	// ActionAllow forwards the traffic to its original destination.
	ActionAllow Action = "allow"
	// ActionDeny refuses the traffic and records it.
	ActionDeny Action = "deny"
	// ActionLogAndDeny denies and marks the record as worth attention, for example a DNS TXT
	// query, a common out-of-band channel.
	ActionLogAndDeny Action = "log_and_deny"
)

// Known reports whether a is an action this gateway implements.
func (a Action) Known() bool {
	switch a {
	case ActionAllow, ActionDeny, ActionLogAndDeny:
		return true
	default:
		return false
	}
}

// Denies reports whether the action refuses the traffic.
func (a Action) Denies() bool { return a == ActionDeny || a == ActionLogAndDeny }

// Match is a rule's condition. A zero field means "any"; all set fields must hold.
type Match struct {
	// Host is an exact name or a `*.suffix` wildcard (any subdomain of suffix, one or more
	// labels). Matched case-insensitively against a connection's SNI or HTTP host, or a DNS
	// query name. A rule with Host set never matches a connection that offered no name.
	Host string
	// Prefix matches the original destination IP. Matched against a connection made straight to
	// an IP, or any connection, not against a name.
	Prefix netip.Prefix
	// Port is the destination port; 0 means any.
	Port uint16
	// Protocol is "tcp", "udp", or "dns"; empty means any.
	Protocol string
	// Method is an HTTP method; empty means any.
	Method string
	// PathPrefix matches the start of an HTTP request path; empty means any.
	PathPrefix string
	// QType is a DNS query type such as "A" or "TXT"; empty means any.
	QType string
}

// Rule is one policy entry. The first rule that matches decides.
type Rule struct {
	ID     string
	Match  Match
	Action Action
}

// Policy is the compiled network policy for a run. It is fixed for the life of the run.
type Policy struct {
	Rules         []Rule
	DefaultEgress Action
}

// Request is one thing to decide on: a TCP connection, a UDP flow, or a DNS query, described by
// what the gateway learned before forwarding it.
type Request struct {
	Protocol string
	DstIP    netip.Addr
	DstPort  uint16
	// Host is the SNI, the HTTP host, or the DNS query name; empty when none was offered.
	Host   string
	Method string
	Path   string
	QType  string
}

// Decide returns the action for req and the id of the rule that chose it. The id is empty when
// the default egress applied.
func (p *Policy) Decide(req Request) (Action, string) {
	for _, r := range p.Rules {
		if r.matches(req) {
			return r.Action, r.ID
		}
	}
	return p.DefaultEgress, ""
}

func (r *Rule) matches(req Request) bool {
	m := r.Match
	if m.Protocol != "" && m.Protocol != req.Protocol {
		return false
	}
	if m.Port != 0 && m.Port != req.DstPort {
		return false
	}
	if m.Host != "" && !matchHost(m.Host, req.Host) {
		return false
	}
	if m.Prefix.IsValid() && !m.Prefix.Contains(req.DstIP) {
		return false
	}
	if m.Method != "" && !strings.EqualFold(m.Method, req.Method) {
		return false
	}
	if m.PathPrefix != "" && !strings.HasPrefix(req.Path, m.PathPrefix) {
		return false
	}
	if m.QType != "" && !strings.EqualFold(m.QType, req.QType) {
		return false
	}
	return true
}

// matchHost matches a host against an exact name or a `*.suffix` wildcard. An empty host never
// matches a non-empty pattern, so a host rule does not catch an IP-only connection.
func matchHost(pattern, host string) bool {
	if host == "" {
		return false
	}
	pattern, host = strings.ToLower(strings.TrimSuffix(pattern, ".")), strings.ToLower(strings.TrimSuffix(host, "."))
	if suffix, ok := strings.CutPrefix(pattern, "*."); ok {
		return strings.HasSuffix(host, "."+suffix) && len(host) > len(suffix)+1
	}
	return pattern == host
}

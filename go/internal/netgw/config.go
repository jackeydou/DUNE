package netgw

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"regexp"
	"slices"
	"strings"
)

// runIDPattern matches the sandbox service's run ids.
var runIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// sandboxIDPattern matches the case format's sandbox ids.
var sandboxIDPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

// Config is net-gateway's on-disk configuration, written by sandboxd from the env the worker
// compiled and mounted read-only. It is fixed for the life of the run; a different policy is a
// different run. All address fields are JSON strings (netip text form).
type Config struct {
	RunID string `json:"run_id"`
	// Gateway is the address net-gateway holds on every sandbox network, their default route.
	Gateway netip.Addr `json:"gateway"`
	// Sandboxes maps each sandbox's subnet to its id, so traffic is attributed by source.
	Sandboxes []SandboxNet `json:"sandboxes"`
	// Resolvers are the upstream DNS servers used for allowed names. Empty disables upstream
	// resolution: every name that a rule does not route is answered NXDOMAIN.
	Resolvers []netip.AddrPort `json:"resolvers"`
	Policy    PolicyConfig     `json:"policy"`
	Platform  PlatformConfig   `json:"platform"`
}

// SandboxNet ties a sandbox id to the subnet of its network.
type SandboxNet struct {
	SandboxID string       `json:"sandbox_id"`
	Subnet    netip.Prefix `json:"subnet"`
}

// PlatformConfig is the NetEventsService listener and the mTLS material that pins it to one run's
// worker. The certificate is issued for this run alone, so the stream can carry only this run.
type PlatformConfig struct {
	// Listen is the address the NetEventsService gRPC server binds, on the platform link only.
	Listen string `json:"listen"`
	// CertFile and KeyFile are net-gateway's server certificate, issued per run.
	CertFile string `json:"cert_file"`
	KeyFile  string `json:"key_file"`
	// ClientCAFile verifies the worker's client certificate.
	ClientCAFile string `json:"client_ca_file"`
}

// PolicyConfig is the network policy in its on-disk form.
type PolicyConfig struct {
	// DefaultEgress is the action for traffic no rule matches. Required; normally "deny".
	DefaultEgress string       `json:"default_egress"`
	Rules         []RuleConfig `json:"rules"`
}

// RuleConfig is one policy rule in its on-disk form. Empty condition fields mean "any".
type RuleConfig struct {
	ID         string `json:"id"`
	Host       string `json:"host"`
	CIDR       string `json:"cidr"`
	Port       int    `json:"port"`
	Protocol   string `json:"protocol"`
	Method     string `json:"method"`
	PathPrefix string `json:"path_prefix"`
	QType      string `json:"qtype"`
	Action     string `json:"action"`
}

// Load reads and validates a config file.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read net-gateway config %s: %w", path, err)
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	var cfg Config
	if err := dec.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("parse net-gateway config %s: %w", path, err)
	}
	if err := cfg.validate(); err != nil {
		return nil, fmt.Errorf("net-gateway config %s: %w", path, err)
	}
	return &cfg, nil
}

func (c *Config) validate() error {
	if !runIDPattern.MatchString(c.RunID) {
		return fmt.Errorf("run_id %q must match %s", c.RunID, runIDPattern)
	}
	if !c.Gateway.IsValid() {
		return fmt.Errorf("gateway address is missing or invalid")
	}
	if len(c.Sandboxes) == 0 {
		return fmt.Errorf("no sandboxes listed; the gateway would attribute no traffic")
	}
	seen := map[string]bool{}
	for i, s := range c.Sandboxes {
		if !sandboxIDPattern.MatchString(s.SandboxID) {
			return fmt.Errorf("sandboxes[%d]: sandbox_id %q must match %s", i, s.SandboxID, sandboxIDPattern)
		}
		if seen[s.SandboxID] {
			return fmt.Errorf("sandboxes[%d]: sandbox_id %q listed twice", i, s.SandboxID)
		}
		seen[s.SandboxID] = true
		if !s.Subnet.IsValid() {
			return fmt.Errorf("sandboxes[%d] (%s): subnet is missing or invalid", i, s.SandboxID)
		}
		if s.Subnet.Addr() != s.Subnet.Masked().Addr() {
			return fmt.Errorf("sandboxes[%d] (%s): subnet %s has host bits set; want the network address", i, s.SandboxID, s.Subnet)
		}
	}
	if c.Platform.Listen == "" {
		return fmt.Errorf("platform.listen is required")
	}
	for _, f := range []struct{ name, val string }{
		{"platform.cert_file", c.Platform.CertFile},
		{"platform.key_file", c.Platform.KeyFile},
		{"platform.client_ca_file", c.Platform.ClientCAFile},
	} {
		if f.val == "" {
			return fmt.Errorf("%s is required", f.name)
		}
	}
	_, err := c.CompilePolicy()
	return err
}

// Sandbox returns the id of the sandbox whose subnet contains ip, and whether one did.
func (c *Config) Sandbox(ip netip.Addr) (string, bool) {
	for _, s := range c.Sandboxes {
		if s.Subnet.Contains(ip) {
			return s.SandboxID, true
		}
	}
	return "", false
}

// CompilePolicy turns the on-disk policy into the runtime Policy, validating every field.
func (c *Config) CompilePolicy() (*Policy, error) {
	def := Action(c.Policy.DefaultEgress)
	if !def.Denies() {
		// The default must fail closed: a run must not silently allow unmatched traffic.
		return nil, fmt.Errorf("policy.default_egress %q must be `deny` or `log_and_deny`", c.Policy.DefaultEgress)
	}
	ids := map[string]bool{}
	rules := make([]Rule, len(c.Policy.Rules))
	for i, rc := range c.Policy.Rules {
		if rc.ID == "" {
			return nil, fmt.Errorf("policy.rules[%d]: id is required", i)
		}
		if ids[rc.ID] {
			return nil, fmt.Errorf("policy.rules[%d]: id %q is used twice", i, rc.ID)
		}
		ids[rc.ID] = true
		r, err := rc.compile()
		if err != nil {
			return nil, fmt.Errorf("policy.rules[%d] (%s): %w", i, rc.ID, err)
		}
		rules[i] = r
	}
	return &Policy{Rules: rules, DefaultEgress: def}, nil
}

var protocols = []string{"", "tcp", "udp", "dns"}

func (rc RuleConfig) compile() (Rule, error) {
	act := Action(rc.Action)
	if !act.Known() {
		return Rule{}, fmt.Errorf("action %q is not one of allow, deny, log_and_deny", rc.Action)
	}
	if !slices.Contains(protocols, rc.Protocol) {
		return Rule{}, fmt.Errorf("protocol %q is not one of tcp, udp, dns", rc.Protocol)
	}
	if rc.Port < 0 || rc.Port > 65535 {
		return Rule{}, fmt.Errorf("port %d is out of range", rc.Port)
	}
	m := Match{
		Host:       rc.Host,
		Port:       uint16(rc.Port),
		Protocol:   rc.Protocol,
		Method:     rc.Method,
		PathPrefix: rc.PathPrefix,
		QType:      rc.QType,
	}
	if rc.CIDR != "" {
		p, err := netip.ParsePrefix(rc.CIDR)
		if err != nil {
			return Rule{}, fmt.Errorf("cidr %q: %w", rc.CIDR, err)
		}
		m.Prefix = p.Masked()
	}
	if m.Host != "" && m.Prefix.IsValid() {
		return Rule{}, fmt.Errorf("set host or cidr, not both: host matches a name, cidr matches an IP")
	}
	return Rule{ID: rc.ID, Match: m, Action: act}, nil
}

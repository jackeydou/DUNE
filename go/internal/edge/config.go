// Package edge is the public entry point: it authenticates callers and serves the public API
// (swarmeval.api.v1), forwarding run calls to the orchestrator's Control API. Behavior:
// docs/services/edge.md.
package edge

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// MaxBundleBytes caps a submitted case bundle, as the Control API does.
const MaxBundleBytes = 64 << 20

// maxRequestBytes caps a RunService request body. A bundle travels base64 in JSON, a third
// larger.
const maxRequestBytes = MaxBundleBytes*4/3 + 1<<20

// maxAccountRequestBytes caps AuthService and UserService request bodies: usernames, passwords,
// and token names.
const maxAccountRequestBytes = 64 << 10

type Config struct {
	// PublicURL is the address browsers use to reach edge. Its origin is the only one whose
	// cookie-authenticated requests are accepted, and an https URL makes the cookie Secure.
	PublicURL *url.URL
	// SessionIdle ends a session not used for this long; SessionMaxAge ends any session this
	// long after sign-in.
	SessionIdle   time.Duration
	SessionMaxAge time.Duration
	// BodyTimeout bounds how long a request body may take to arrive, so a client cannot hold a
	// connection by dripping one; UploadTimeout replaces it for the calls that carry case
	// bundles. Neither limits how long the answer takes, so event streams outlive both.
	BodyTimeout   time.Duration
	UploadTimeout time.Duration
}

// Request body deadlines edge serves with.
const (
	DefaultBodyTimeout   = 30 * time.Second
	DefaultUploadTimeout = 5 * time.Minute
)

// ParsePublicURL accepts an http or https URL with a host and nothing past the path `/`, and
// returns it as browsers serialize an origin: host in lower case, no default port. Otherwise
// `https://Swarm.example.com:443` would never equal the Origin browsers send.
func ParsePublicURL(raw string) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("public URL %q: %w", raw, err)
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || u.User != nil {
		return nil, fmt.Errorf("public URL %q: want http(s)://host[:port], the address browsers use to reach edge, with no path, query, or credentials", raw)
	}
	host, port := strings.ToLower(u.Hostname()), u.Port()
	if (u.Scheme == "https" && port == "443") || (u.Scheme == "http" && port == "80") {
		port = ""
	}
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	if port != "" {
		host += ":" + port
	}
	return &url.URL{Scheme: u.Scheme, Host: host}, nil
}

// origin is the value browsers send in the Origin header for pages served from PublicURL.
func (c Config) origin() string {
	return c.PublicURL.Scheme + "://" + c.PublicURL.Host
}

func (c Config) secure() bool {
	return c.PublicURL.Scheme == "https"
}

// cookieName uses the `__Host-` prefix when the cookie can be Secure: browsers then refuse it
// unless it is Secure, host-only, and for path `/`.
func (c Config) cookieName() string {
	if c.secure() {
		return "__Host-swarm_session"
	}
	return "swarm_session"
}

func (c Config) sessionCookie(value string, expires time.Time) *http.Cookie {
	return &http.Cookie{
		Name:     c.cookieName(),
		Value:    value,
		Path:     "/",
		Expires:  expires,
		HttpOnly: true,
		Secure:   c.secure(),
		SameSite: http.SameSiteStrictMode,
	}
}

func (c Config) clearedCookie() *http.Cookie {
	cookie := c.sessionCookie("", time.Unix(0, 0))
	cookie.MaxAge = -1
	return cookie
}

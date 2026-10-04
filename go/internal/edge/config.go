// Package edge is the public entry point: it authenticates callers and serves the public API
// (swarmeval.api.v1), forwarding run calls to the orchestrator's Control API. Behavior:
// docs/services/edge.md.
package edge

import (
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// MaxBundleBytes caps a submitted case bundle, as the Control API does.
const MaxBundleBytes = 64 << 20

// maxRequestBytes caps a request body. A bundle travels base64 in JSON, a third larger.
const maxRequestBytes = MaxBundleBytes*4/3 + 1<<20

type Config struct {
	// PublicURL is the address browsers use to reach edge. Its origin is the only one whose
	// cookie-authenticated requests are accepted, and an https URL makes the cookie Secure.
	PublicURL *url.URL
	// SessionIdle ends a session not used for this long; SessionMaxAge ends any session this
	// long after sign-in.
	SessionIdle   time.Duration
	SessionMaxAge time.Duration
}

// ParsePublicURL accepts an http or https URL with a host and nothing past the path `/`.
func ParsePublicURL(raw string) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("public URL %q: %w", raw, err)
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || u.User != nil {
		return nil, fmt.Errorf("public URL %q: want http(s)://host[:port], the address browsers use to reach edge, with no path, query, or credentials", raw)
	}
	return u, nil
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

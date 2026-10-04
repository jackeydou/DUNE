package tenant

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base32"
	"encoding/hex"
	"strings"
)

// TokenPrefix starts every API token, so a leaked one is recognizable in logs and by secret
// scanners.
const TokenPrefix = "swm_"

var b32 = base32.StdEncoding.WithPadding(base32.NoPadding)

// NewSessionSecret returns a session cookie value: 32 random bytes, base32.
func NewSessionSecret() string {
	return strings.ToLower(b32.EncodeToString(randomBytes(32)))
}

// NewToken returns an API token, `swm_` and 32 random bytes in base32, and its public id.
func NewToken() (token, tokenID string) {
	return TokenPrefix + strings.ToLower(b32.EncodeToString(randomBytes(32))), "tok_" + hex.EncodeToString(randomBytes(8))
}

// SecretHash is what the database stores for a session secret or token: its sha256. The secrets
// carry 256 random bits, so a fast hash is enough.
func SecretHash(secret string) []byte {
	sum := sha256.Sum256([]byte(secret))
	return sum[:]
}

func randomBytes(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b) // crypto/rand.Read never returns an error since Go 1.24
	return b
}

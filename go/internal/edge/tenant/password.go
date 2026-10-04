package tenant

import (
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"golang.org/x/crypto/argon2"
)

// Argon2id parameters: RFC 9106's second recommended option (64 MiB, 3 passes, 4 lanes).
const (
	argonMemoryKiB = 64 * 1024
	argonTime      = 3
	argonThreads   = 4
	argonKeyLen    = 32
	argonSaltLen   = 16
)

// Password length limits, in characters. The upper bound keeps a sign-in from feeding megabytes
// into the hash.
const (
	MinPasswordLen = 12
	MaxPasswordLen = 1024
)

// ErrMismatch is a password that does not match its hash.
var ErrMismatch = errors.New("password does not match")

var b64 = base64.RawStdEncoding

// CheckPassword reports why a new password is refused, or nil.
func CheckPassword(password string) error {
	n := utf8.RuneCountInString(password)
	if n < MinPasswordLen || n > MaxPasswordLen {
		return fmt.Errorf("a password must be %d to %d characters long; this one has %d", MinPasswordLen, MaxPasswordLen, n)
	}
	return nil
}

// HashPassword returns the argon2id hash of password as a PHC string:
// `$argon2id$v=19$m=65536,t=3,p=4$<salt>$<key>`, base64 without padding.
func HashPassword(password string) string {
	salt := randomBytes(argonSaltLen)
	key := argon2.IDKey([]byte(password), salt, argonTime, argonMemoryKiB, argonThreads, argonKeyLen)
	return fmt.Sprintf("$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemoryKiB, argonTime, argonThreads, b64.EncodeToString(salt), b64.EncodeToString(key))
}

// VerifyPassword returns nil when password matches hash, ErrMismatch when it does not, and
// another error when hash is not an argon2id PHC string. The parameters are read from the hash,
// so hashes made with older parameters still verify.
func VerifyPassword(password, hash string) error {
	parts := strings.Split(hash, "$")
	// "", "argon2id", "v=19", "m=…,t=…,p=…", salt, key
	if len(parts) != 6 || parts[0] != "" || parts[1] != "argon2id" {
		return errors.New("stored password hash is not an argon2id PHC string")
	}
	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil || version != argon2.Version {
		return fmt.Errorf("stored password hash has argon2 version %q; want v=%d", parts[2], argon2.Version)
	}
	var memory, time uint32
	var threads uint8
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &memory, &time, &threads); err != nil {
		return fmt.Errorf("stored password hash parameters %q: %w", parts[3], err)
	}
	salt, err := b64.DecodeString(parts[4])
	if err != nil {
		return fmt.Errorf("stored password hash salt: %w", err)
	}
	want, err := b64.DecodeString(parts[5])
	if err != nil || len(want) == 0 {
		return errors.New("stored password hash key is not base64")
	}
	got := argon2.IDKey([]byte(password), salt, time, memory, threads, uint32(len(want)))
	if subtle.ConstantTimeCompare(got, want) != 1 {
		return ErrMismatch
	}
	return nil
}

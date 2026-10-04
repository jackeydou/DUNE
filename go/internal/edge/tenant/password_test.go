package tenant

import (
	"errors"
	"strings"
	"testing"

	"golang.org/x/crypto/argon2"
)

func TestHashPasswordVerifiesOnlyItsPassword(t *testing.T) {
	hash := HashPassword("correct horse battery staple")
	if !strings.HasPrefix(hash, "$argon2id$v=19$m=65536,t=3,p=4$") {
		t.Fatalf("hash %q is not an argon2id PHC string with RFC 9106 parameters", hash)
	}
	if err := VerifyPassword("correct horse battery staple", hash); err != nil {
		t.Fatalf("the right password: %v", err)
	}
	if err := VerifyPassword("correct horse battery stapler", hash); !errors.Is(err, ErrMismatch) {
		t.Fatalf("a wrong password: got %v, want ErrMismatch", err)
	}
}

func TestHashPasswordSaltsEachHash(t *testing.T) {
	first, second := HashPassword("same password here"), HashPassword("same password here")
	if first == second {
		t.Fatal("two hashes of one password are equal; each must have its own salt")
	}
}

func TestVerifyPasswordReadsParametersFromTheHash(t *testing.T) {
	// Weaker parameters than today's (8 KiB, 1 pass, 1 lane): a hash made with them still verifies.
	salt := []byte("0123456789abcdef")
	key := argon2.IDKey([]byte("an older password"), salt, 1, 8, 1, 32)
	old := "$argon2id$v=19$m=8,t=1,p=1$" + b64.EncodeToString(salt) + "$" + b64.EncodeToString(key)
	if err := VerifyPassword("an older password", old); err != nil {
		t.Fatalf("a hash with older parameters: %v", err)
	}
	if err := VerifyPassword("another password", old); !errors.Is(err, ErrMismatch) {
		t.Fatalf("a wrong password against it: got %v, want ErrMismatch", err)
	}
}

func TestVerifyPasswordRefusesMalformedHashes(t *testing.T) {
	for _, hash := range []string{
		"",
		"plaintext",
		"$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$a2V5",
		"$argon2id$v=16$m=65536,t=3,p=4$c2FsdA$a2V5",
		"$argon2id$v=19$m=x,t=3,p=4$c2FsdA$a2V5",
		"$argon2id$v=19$m=65536,t=3,p=4$!!$a2V5",
		"$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$",
	} {
		if err := VerifyPassword("password", hash); err == nil || errors.Is(err, ErrMismatch) {
			t.Errorf("hash %q: got %v, want an error saying the hash is malformed", hash, err)
		}
	}
}

func TestCheckPasswordLength(t *testing.T) {
	if err := CheckPassword(strings.Repeat("密", 12)); err != nil {
		t.Errorf("12 characters (36 bytes) is long enough: %v", err)
	}
	if err := CheckPassword("short"); err == nil {
		t.Error("5 characters was accepted")
	}
	if err := CheckPassword(strings.Repeat("a", MaxPasswordLen+1)); err == nil {
		t.Error("a password over the limit was accepted")
	}
}

func TestCheckUsername(t *testing.T) {
	for _, ok := range []string{"ada", "a", "grace.hopper", "x_1-2", strings.Repeat("a", 64)} {
		if err := CheckUsername(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"", "Ada", "-ada", ".ada", "ada lovelace", "ada@x", strings.Repeat("a", 65)} {
		if CheckUsername(bad) == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}

func TestTokensAreDistinctAndPrefixed(t *testing.T) {
	a, idA := NewToken()
	b, idB := NewToken()
	if a == b || idA == idB {
		t.Fatal("two tokens are equal")
	}
	if !strings.HasPrefix(a, TokenPrefix) || len(a) != len(TokenPrefix)+52 {
		t.Fatalf("token %q: want swm_ and 52 base32 characters", a)
	}
	if string(SecretHash(a)) == string(SecretHash(b)) {
		t.Fatal("two tokens hash alike")
	}
}

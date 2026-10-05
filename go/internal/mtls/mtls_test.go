package mtls_test

// The handshake rules are tested in internal/certs, which issues the certificates they need.

import (
	"testing"

	"github.com/jackeydou/DUNE/go/internal/mtls"
)

func TestFilesGoTogether(t *testing.T) {
	if err := (mtls.Files{}).Check(); err != nil {
		t.Errorf("no files: %v", err)
	}
	if err := (mtls.Files{Cert: "c", Key: "k"}).Check(); err == nil {
		t.Error("a certificate without a CA was accepted")
	}
	if err := (mtls.Files{CA: "ca"}).Check(); err == nil {
		t.Error("a CA without a certificate was accepted")
	}
}

func TestIsLoopback(t *testing.T) {
	for address, want := range map[string]bool{
		"127.0.0.1:7071": true, "localhost:7071": true, "[::1]:7071": true,
		"0.0.0.0:7071": false, ":7071": false, "10.0.0.5:7071": false, "sandboxd:7071": false, "7071": false,
	} {
		if got := mtls.IsLoopback(address); got != want {
			t.Errorf("%s: loopback = %v, want %v", address, got, want)
		}
	}
}

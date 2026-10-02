package netgw

import (
	"crypto/tls"
	"net"
	"testing"
	"time"
)

// clientHelloBytes captures the ClientHello a stdlib TLS client sends for serverName.
func clientHelloBytes(t *testing.T, serverName string) []byte {
	t.Helper()
	client, server := net.Pipe()
	defer func() { _ = client.Close() }()
	captured := make(chan []byte, 1)
	go func() {
		buf := make([]byte, 4096)
		_ = server.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, _ := server.Read(buf)
		captured <- buf[:n]
		_ = server.Close()
	}()
	cfg := &tls.Config{ServerName: serverName, MinVersion: tls.VersionTLS12}
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	// Handshake fails once the server pipe closes; we only need the first flight it wrote.
	_ = tls.Client(client, cfg).Handshake()
	return <-captured
}

func TestClassifyTLS(t *testing.T) {
	hello := clientHelloBytes(t, "api.github.com")
	got := classifyTCP(hello)
	if got.Kind != "tls" || got.SNI != "api.github.com" {
		t.Fatalf("classifyTCP(ClientHello) = %+v; want tls / api.github.com", got)
	}
	if got.host() != "api.github.com" {
		t.Fatalf("host() = %q", got.host())
	}
}

func TestClassifyTLSPartialRecordIsRaw(t *testing.T) {
	hello := clientHelloBytes(t, "api.github.com")
	// A record not yet fully present must not be read as a (truncated) ClientHello.
	if got := classifyTCP(hello[:10]); got.Kind != "raw" {
		t.Fatalf("partial ClientHello classified as %q; want raw", got.Kind)
	}
}

func TestClassifyHTTP(t *testing.T) {
	req := []byte("GET /deaddrop/page?x=1 HTTP/1.1\r\nHost: wiki.prowiki.test\r\nUser-Agent: curl\r\n\r\n")
	got := classifyTCP(req)
	if got.Kind != "http" || got.Method != "GET" || got.Host != "wiki.prowiki.test" || got.Path != "/deaddrop/page?x=1" {
		t.Fatalf("classifyTCP(http) = %+v", got)
	}
}

func TestClassifyHTTPPartialLineIsRaw(t *testing.T) {
	if got := classifyTCP([]byte("GET /partial")); got.Kind != "raw" {
		t.Fatalf("partial request line classified as %q; want raw", got.Kind)
	}
}

func TestClassifyRaw(t *testing.T) {
	for _, b := range [][]byte{
		{},
		[]byte("SSH-2.0-OpenSSH_9.6\r\n"),
		{0x16, 0x03}, // too short for a TLS record header
		[]byte("NOTAMETHOD / HTTP/1.1\r\n\r\n"),
	} {
		if got := classifyTCP(b); got.Kind != "raw" {
			t.Errorf("classifyTCP(%q) = %q; want raw", b, got.Kind)
		}
	}
}

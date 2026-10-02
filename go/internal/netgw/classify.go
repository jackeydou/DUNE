package netgw

import (
	"bytes"
	"encoding/binary"
	"strings"
)

// Classification is what a TCP connection's first bytes looked like.
type Classification struct {
	// Kind is "tls", "http", or "raw".
	Kind string
	// SNI is the server name from a TLS ClientHello, for Kind "tls".
	SNI string
	// Host, Method, and Path are the first HTTP request's, for Kind "http".
	Host   string
	Method string
	Path   string
}

// Host returns the name the connection offered: the SNI, the HTTP host, or empty.
func (c Classification) host() string {
	switch c.Kind {
	case "tls":
		return c.SNI
	case "http":
		return c.Host
	default:
		return ""
	}
}

// classifyTCP peeks at the first bytes of a connection without consuming them and reports what
// protocol they look like. It never errors: bytes it cannot read as TLS or HTTP are "raw".
func classifyTCP(peek []byte) Classification {
	if sni, ok := clientHelloSNI(peek); ok {
		return Classification{Kind: "tls", SNI: sni}
	}
	if method, host, path, ok := httpRequestLine(peek); ok {
		return Classification{Kind: "http", Method: method, Host: host, Path: path}
	}
	return Classification{Kind: "raw"}
}

// httpMethods are the request methods whose presence marks a plaintext HTTP connection.
var httpMethods = []string{"GET ", "POST ", "PUT ", "DELETE ", "HEAD ", "OPTIONS ", "PATCH ", "TRACE ", "CONNECT "}

// httpRequestLine parses the method, host, and path from the start of an HTTP request. It reads
// only what is in peek, so a request whose headers are not yet all present still yields the
// method and path, with the host it has seen so far.
func httpRequestLine(peek []byte) (method, host, path string, ok bool) {
	starts := false
	for _, m := range httpMethods {
		if bytes.HasPrefix(peek, []byte(m)) {
			starts = true
			break
		}
	}
	if !starts {
		return "", "", "", false
	}
	// A request line must be terminated before we trust it; without a CRLF the peek is partial.
	line, _, found := bytes.Cut(peek, []byte("\r\n"))
	if !found {
		return "", "", "", false
	}
	fields := strings.Fields(string(line))
	if len(fields) != 3 || !strings.HasPrefix(fields[2], "HTTP/") {
		return "", "", "", false
	}
	return fields[0], hostHeader(peek), fields[1], true
}

// hostHeader scans the peeked request for the first Host header value.
func hostHeader(peek []byte) string {
	for _, raw := range bytes.Split(peek, []byte("\r\n")) {
		name, value, found := bytes.Cut(raw, []byte(":"))
		if found && strings.EqualFold(strings.TrimSpace(string(name)), "host") {
			return strings.TrimSpace(string(value))
		}
	}
	return ""
}

// clientHelloSNI extracts the SNI from a TLS ClientHello record. It returns ok=false for bytes
// that are not a ClientHello, or when the record is not yet fully present in peek.
func clientHelloSNI(peek []byte) (string, bool) {
	// TLS record: type(1)=0x16 handshake, version(2), length(2).
	if len(peek) < 5 || peek[0] != 0x16 {
		return "", false
	}
	recLen := int(binary.BigEndian.Uint16(peek[3:5]))
	body := peek[5:]
	if len(body) < recLen {
		return "", false
	}
	body = body[:recLen]
	// Handshake: msg_type(1)=0x01 ClientHello, length(3).
	if len(body) < 4 || body[0] != 0x01 {
		return "", false
	}
	hs := body[4:]
	// version(2) + random(32).
	if len(hs) < 34 {
		return "", false
	}
	hs = hs[34:]
	// session_id: len(1) + bytes.
	hs, ok := skipVector8(hs)
	if !ok {
		return "", false
	}
	// cipher_suites: len(2) + bytes.
	hs, ok = skipVector16(hs)
	if !ok {
		return "", false
	}
	// compression_methods: len(1) + bytes.
	hs, ok = skipVector8(hs)
	if !ok {
		return "", false
	}
	// extensions: len(2) + bytes.
	if len(hs) < 2 {
		return "", false
	}
	extLen := int(binary.BigEndian.Uint16(hs))
	hs = hs[2:]
	if len(hs) < extLen {
		return "", false
	}
	return sniFromExtensions(hs[:extLen])
}

func sniFromExtensions(ext []byte) (string, bool) {
	for len(ext) >= 4 {
		extType := binary.BigEndian.Uint16(ext)
		extDataLen := int(binary.BigEndian.Uint16(ext[2:]))
		ext = ext[4:]
		if len(ext) < extDataLen {
			return "", false
		}
		data := ext[:extDataLen]
		ext = ext[extDataLen:]
		if extType != 0x0000 { // server_name
			continue
		}
		// ServerNameList: list_len(2), then entries of name_type(1) + name(len16).
		if len(data) < 2 {
			return "", false
		}
		list := data[2:]
		for len(list) >= 3 {
			nameType := list[0]
			nameLen := int(binary.BigEndian.Uint16(list[1:]))
			list = list[3:]
			if len(list) < nameLen {
				return "", false
			}
			if nameType == 0x00 { // host_name
				return string(list[:nameLen]), true
			}
			list = list[nameLen:]
		}
	}
	return "", false
}

func skipVector8(b []byte) ([]byte, bool) {
	if len(b) < 1 {
		return nil, false
	}
	n := int(b[0])
	if len(b) < 1+n {
		return nil, false
	}
	return b[1+n:], true
}

func skipVector16(b []byte) ([]byte, bool) {
	if len(b) < 2 {
		return nil, false
	}
	n := int(binary.BigEndian.Uint16(b))
	if len(b) < 2+n {
		return nil, false
	}
	return b[2+n:], true
}

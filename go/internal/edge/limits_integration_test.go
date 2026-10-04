//go:build integration

package edge

import (
	"bufio"
	"errors"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
)

// Review on #19: a body dripped after its headers held a connection forever.
func TestADrippedBodyIsCutOff(t *testing.T) {
	s := newStack(t)
	conn, err := net.Dial("tcp", strings.TrimPrefix(s.url, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	start := time.Now()
	if _, err := conn.Write([]byte("POST /swarmeval.api.v1.AuthService/Login HTTP/1.1\r\nHost: edge\r\n" +
		"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{")); err != nil {
		t.Fatal(err)
	}
	if err := conn.SetReadDeadline(time.Now().Add(10 * testBodyTimeout)); err != nil {
		t.Fatal(err)
	}
	res, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err == nil {
		_ = res.Body.Close()
		if res.StatusCode == http.StatusOK {
			t.Fatal("a body that never arrived was answered 200")
		}
	} else if ne := net.Error(nil); errors.As(err, &ne) && ne.Timeout() {
		t.Fatalf("edge still held the connection after %s", time.Since(start))
	}
	if waited := time.Since(start); waited < testBodyTimeout {
		t.Fatalf("cut off after %s, before the %s body deadline", waited, testBodyTimeout)
	}
}

// The body deadline must not cancel a call still answering, such as an event stream: HTTP/2
// enforces it per stream, and HTTP/1.1 through its background read.
func TestAStreamOutlivesTheBodyDeadline(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)
	submitted, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("tar")}))
	if err != nil {
		t.Fatal(err)
	}
	s.control.delay = 4 * testBodyTimeout
	s.control.events = []*controlv1.StreamEventsResponse{{Seq: 1, EventId: "e1", Type: "model"}}

	for name, protocols := range map[string]func(*http.Protocols){
		"HTTP/1.1": func(p *http.Protocols) { p.SetHTTP1(true) },
		"HTTP/2":   func(p *http.Protocols) { p.SetUnencryptedHTTP2(true) },
	} {
		t.Run(name, func(t *testing.T) {
			var p http.Protocols
			protocols(&p)
			client := apiv1connect.NewRunServiceClient(&http.Client{Transport: &http.Transport{Protocols: &p}}, s.url)
			stream, err := client.StreamEvents(t.Context(), bearer(token, &apiv1.StreamEventsRequest{RunId: submitted.Msg.GetRunIds()[0]}))
			if err != nil {
				t.Fatal(err)
			}
			n := 0
			for stream.Receive() {
				n++
			}
			if err := stream.Err(); err != nil || n != 1 {
				t.Fatalf("after %s: %d events, %v", s.control.delay, n, err)
			}
		})
	}
}

// Review on #19: an unauthenticated caller could have edge keep a username of any length as a
// throttle key.
func TestOverlongUsernamesAreNotKept(t *testing.T) {
	s := newStack(t)
	auth := newAuthService(s.store, s.cfg, quietLog())
	long := strings.Repeat("a", 60_000)
	for range 3 {
		_, err := auth.signIn(t.Context(), long, pw, "10.0.0.9:5000")
		wantCode(t, err, connect.CodeUnauthenticated)
	}
	if len(auth.limiter.entries) != 1 || auth.limiter.entries["addr:10.0.0.9"] == nil {
		t.Fatalf("throttle keys %d, want only the address", len(auth.limiter.entries))
	}
	for range freeFailures - 3 {
		_, _ = auth.signIn(t.Context(), "NOT VALID", pw, "10.0.0.9:5000")
	}
	_, err := auth.signIn(t.Context(), long, pw, "10.0.0.9:5000")
	wantCode(t, err, connect.CodeResourceExhausted)

	_, err = s.auth.Login(t.Context(), connect.NewRequest(&apiv1.LoginRequest{Username: strings.Repeat("a", 1<<20), Password: pw}))
	wantCode(t, err, connect.CodeResourceExhausted)
}

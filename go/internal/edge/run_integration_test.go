//go:build integration

package edge

import (
	"errors"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
)

func TestRunCallsCarryTheCallerAsActor(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)
	b := s.login(t, "ada", pw)

	submitted, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("tar"), Epochs: 2, Suite: "core"}))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.control.last().(*controlv1.SubmitRunsRequest)
	if sent.GetActor() != "ada" || sent.GetEpochs() != 2 || sent.GetSuite() != "core" || string(sent.GetCaseBundle()) != "tar" {
		t.Fatalf("Control API got %v", sent)
	}
	runID := submitted.Msg.GetRunIds()[0]

	got, err := s.runs.GetRun(t.Context(), asBrowser(b, &apiv1.GetRunRequest{RunId: runID}))
	if err != nil {
		t.Fatal(err)
	}
	run := got.Msg.GetRun()
	if run.GetSubmittedBy() != "ada" || run.GetWorkspace() != "safety" || run.GetVariantValues().GetFields()["model"].GetStringValue() != "m" {
		t.Fatalf("GetRun: %v", run)
	}

	cancelled, err := s.runs.CancelRun(t.Context(), bearer(token, &apiv1.CancelRunRequest{RunId: runID}))
	if err != nil {
		t.Fatal(err)
	}
	if cancelled.Msg.GetRun().GetCancelledBy() != "ada" {
		t.Fatalf("CancelRun: %v", cancelled.Msg.GetRun())
	}
	resumed, err := s.runs.ResumeRun(t.Context(), bearer(token, &apiv1.ResumeRunRequest{RunId: runID}))
	if err != nil {
		t.Fatal(err)
	}
	if resumed.Msg.GetRun().GetResumedBy() != "ada" {
		t.Fatalf("ResumeRun: %v", resumed.Msg.GetRun())
	}

	forked, err := s.runs.ForkRun(t.Context(), bearer(token, &apiv1.ForkRunRequest{
		RunId: runID, AtEventId: "e9",
		Edits: []*apiv1.ForkEdit{{Edit: &apiv1.ForkEdit_ReplaceDelivery{ReplaceDelivery: &apiv1.ReplaceDelivery{
			SendEventId: "e3", Recipient: "b", Content: "stay quiet",
		}}}},
	}))
	if err != nil {
		t.Fatal(err)
	}
	fork := s.control.last().(*controlv1.ForkRunRequest)
	if fork.GetActor() != "ada" || fork.GetAtEventId() != "e9" || fork.GetEdits()[0].GetReplaceDelivery().GetContent() != "stay quiet" {
		t.Fatalf("Control API got %v", fork)
	}
	if forked.Msg.GetRun().GetForkedFrom() != runID || forked.Msg.GetRun().GetForkSeq() != 7 {
		t.Fatalf("ForkRun: %v", forked.Msg.GetRun())
	}

	listed, err := s.runs.ListRuns(t.Context(), bearer(token, &apiv1.ListRunsRequest{Suite: "core", Limit: 5}))
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Msg.GetRuns()) != 1 || s.control.last().(*controlv1.ListRunsRequest).GetLimit() != 5 {
		t.Fatalf("ListRuns: %v", listed.Msg.GetRuns())
	}
}

func TestASuiteIsForwardedWithItsBundlesAndTheActor(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	res, err := s.runs.SubmitSuite(t.Context(), bearer(token, &apiv1.SubmitSuiteRequest{
		SuiteYaml:   "id: core",
		CaseBundles: map[string][]byte{"../cases/a": []byte("tar a")},
	}))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.control.last().(*controlv1.SubmitSuiteRequest)
	if sent.GetActor() != "ada" || sent.GetSuiteYaml() != "id: core" || string(sent.GetCaseBundles()["../cases/a"]) != "tar a" {
		t.Fatalf("Control API got %v", sent)
	}
	if res.Msg.GetSuite() != "core.0000aaaa" || res.Msg.GetSubmissions()[0].GetRunIds()[0] != "c.s2.v0.e1" {
		t.Fatalf("SubmitSuite: %v", res.Msg)
	}

	_, err = s.runs.SubmitSuite(t.Context(), bearer(token, &apiv1.SubmitSuiteRequest{
		SuiteYaml:   "id: big",
		CaseBundles: map[string][]byte{"a": make([]byte, MaxBundleBytes/2), "b": make([]byte, MaxBundleBytes/2)},
	}))
	wantCode(t, err, connect.CodeInvalidArgument)
}

func TestTheCallersMistakesComeBackAsTheyAre(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("not a tar")}))
	wantCode(t, err, connect.CodeInvalidArgument)
	if !strings.Contains(err.Error(), "not a tar archive") {
		t.Fatalf("the loader's message is lost: %v", err)
	}
	_, err = s.runs.GetRun(t.Context(), bearer(token, &apiv1.GetRunRequest{RunId: "nope"}))
	wantCode(t, err, connect.CodeNotFound)

	submitted, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("tar")}))
	if err != nil {
		t.Fatal(err)
	}
	runID := submitted.Msg.GetRunIds()[0]
	if _, err := s.runs.CancelRun(t.Context(), bearer(token, &apiv1.CancelRunRequest{RunId: runID})); err != nil {
		t.Fatal(err)
	}
	_, err = s.runs.CancelRun(t.Context(), bearer(token, &apiv1.CancelRunRequest{RunId: runID}))
	wantCode(t, err, connect.CodeFailedPrecondition)
}

func TestAPlatformFailureIsNotPassedOn(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)
	s.control.fail(connect.NewError(connect.CodeInternal, errors.New("psycopg.OperationalError: connection to 10.0.0.5 refused")))

	_, err := s.runs.ListRuns(t.Context(), bearer(token, &apiv1.ListRunsRequest{}))
	wantCode(t, err, connect.CodeUnavailable)
	if strings.Contains(err.Error(), "10.0.0.5") {
		t.Fatalf("an internal detail reached the caller: %v", err)
	}
}

func TestATooLargeBundleIsRefusedBeforeTheControlPlane(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: make([]byte, MaxBundleBytes+1)}))
	wantCode(t, err, connect.CodeInvalidArgument)
	if len(s.control.requests) != 0 {
		t.Fatal("the bundle reached the Control API")
	}
}

func TestEventsAreRelayedInOrder(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	b := s.login(t, "ada", pw)
	token := s.token(t, "ada", pw)
	submitted, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("tar")}))
	if err != nil {
		t.Fatal(err)
	}
	for seq := int64(1); seq <= 3; seq++ {
		s.control.events = append(s.control.events, &controlv1.StreamEventsResponse{
			Seq: seq, EventId: "e" + string(rune('0'+seq)), Type: "model", AgentId: "a", PayloadJson: `{"x":1}`, Line: "model: hi",
		})
	}

	stream, err := s.runs.StreamEvents(t.Context(), asBrowser(b, &apiv1.StreamEventsRequest{RunId: submitted.Msg.GetRunIds()[0], AfterSeq: 1}))
	if err != nil {
		t.Fatal(err)
	}
	var seqs []int64
	for stream.Receive() {
		seqs = append(seqs, stream.Msg().GetSeq())
		if stream.Msg().GetPayloadJson() != `{"x":1}` || stream.Msg().GetLine() != "model: hi" {
			t.Fatalf("payload %q", stream.Msg().GetPayloadJson())
		}
	}
	if err := stream.Err(); err != nil {
		t.Fatal(err)
	}
	if len(seqs) != 2 || seqs[0] != 2 || seqs[1] != 3 {
		t.Fatalf("got seqs %v, want [2 3]", seqs)
	}

	unknown, err := s.runs.StreamEvents(t.Context(), bearer(token, &apiv1.StreamEventsRequest{RunId: "nope"}))
	if err != nil {
		t.Fatal(err)
	}
	for unknown.Receive() {
		t.Fatal("events for an unknown run")
	}
	wantCode(t, unknown.Err(), connect.CodeNotFound)
}

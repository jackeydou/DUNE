//go:build integration

package edge

import (
	"bytes"
	"context"
	"errors"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	analysisv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1/apiv1connect"
)

// fakeAnalysis stands in for the analysis service, recording each request.
type fakeAnalysis struct {
	analysisv1connect.UnimplementedAnalysisServiceHandler

	mu       sync.Mutex
	requests []proto.Message
}

func (f *fakeAnalysis) record(msg proto.Message) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, msg)
}

func (f *fakeAnalysis) last() proto.Message {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.requests[len(f.requests)-1]
}

func (f *fakeAnalysis) Query(_ context.Context, req *connect.Request[analysisv1.QueryRequest], out *connect.ServerStream[analysisv1.QueryResponse]) error {
	f.record(req.Msg)
	if strings.HasPrefix(req.Msg.GetSql(), "SET") {
		return connect.NewError(connect.CodeInvalidArgument, errors.New("give one SELECT statement."))
	}
	row, err := structpb.NewList([]any{"probe.s1.v0.e1", 6.0, nil, []any{"a", "b"}})
	if err != nil {
		return err
	}
	for _, chunk := range []*analysisv1.QueryResponse{
		{Columns: []*analysisv1.QueryColumn{{Name: "run_id", Type: "VARCHAR"}, {Name: "events", Type: "BIGINT"}}},
		{Rows: []*structpb.ListValue{row}},
		{Truncated: true},
	} {
		if err := out.Send(chunk); err != nil {
			return err
		}
	}
	return nil
}

func (f *fakeAnalysis) StartRuleScan(_ context.Context, req *connect.Request[analysisv1.StartRuleScanRequest]) (*connect.Response[analysisv1.StartRuleScanResponse], error) {
	f.record(req.Msg)
	return connect.NewResponse(&analysisv1.StartRuleScanResponse{Job: &analysisv1.Job{
		JobId: "j1", Kind: "rule_scan", Status: "queued", Actor: req.Msg.GetActor(),
		CreatedAt: timestamppb.New(time.Unix(1_790_000_000, 0)),
	}}), nil
}

func (f *fakeAnalysis) GetJob(_ context.Context, req *connect.Request[analysisv1.GetJobRequest]) (*connect.Response[analysisv1.GetJobResponse], error) {
	f.record(req.Msg)
	if req.Msg.GetJobId() != "j1" {
		return nil, connect.NewError(connect.CodeNotFound, errors.New("no analysis job `nope`."))
	}
	return connect.NewResponse(&analysisv1.GetJobResponse{Job: &analysisv1.Job{
		JobId: "j1", Kind: "rule_scan", Status: "done", Actor: "ada",
		RuleScan: &analysisv1.RuleScanResult{
			RuleSetSha256: "abc", TotalMatches: 1,
			Runs:    []*analysisv1.RuleScanRun{{RunId: "r1", Matches: 1}},
			Matches: []*analysisv1.RuleMatch{{RunId: "r1", Seq: 6, EventId: "e6", RuleId: "mailbox", Field: "result", Via: []string{"base64"}, Excerpt: "zz"}},
		},
	}}), nil
}

func (f *fakeAnalysis) Report(_ context.Context, req *connect.Request[analysisv1.ReportRequest]) (*connect.Response[analysisv1.ReportResponse], error) {
	f.record(req.Msg)
	stderr := 0.5
	rate := &analysisv1.Rate{CaseId: "c", CaseSha256: "ab", Variant: 1, VariantValuesJson: `{"m":"x"}`, Scorer: "leak", Epochs: 2, Rate: 0.5, Stderr: &stderr, CiLow: 0.1, CiHigh: 0.9}
	value := 1.0
	return connect.NewResponse(&analysisv1.ReportResponse{
		Rates:       []*analysisv1.Rate{rate},
		Unscored:    []*analysisv1.Unscored{{CaseId: "c", Status: "failed", Runs: 3}},
		Coverage:    []*analysisv1.Coverage{{CaseId: "c", Requested: 5, Done: 2, Replaced: 1, Missing: 3}},
		Forks:       []*analysisv1.ForkScore{{RunId: "r.f1", ForkedFrom: "r", ForkSeq: 7, Scorer: "leak", Value: &value}},
		Differences: []*analysisv1.Difference{{CaseId: "c", Scorer: "leak", A: rate, B: rate, Diff: 0.25, CiLow: -0.1, CiHigh: 0.6}},
		Markdown:    "| leak |",
	}), nil
}

func (f *fakeAnalysis) DownloadExport(_ context.Context, req *connect.Request[analysisv1.DownloadExportRequest], out *connect.ServerStream[analysisv1.DownloadExportResponse]) error {
	f.record(req.Msg)
	for _, chunk := range []string{"first-", "second"} {
		if err := out.Send(&analysisv1.DownloadExportResponse{Chunk: []byte(chunk)}); err != nil {
			return err
		}
	}
	return nil
}

func TestAQueryIsRelayedChunkByChunk(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	stream, err := s.analysis.Query(t.Context(), bearer(token, &apiv1.QueryRequest{Sql: "SELECT 1", MaxRows: 5}))
	if err != nil {
		t.Fatal(err)
	}
	var chunks []*apiv1.QueryResponse
	for stream.Receive() {
		chunks = append(chunks, stream.Msg())
	}
	if err := stream.Err(); err != nil {
		t.Fatal(err)
	}
	sent := s.analysisFake.last().(*analysisv1.QueryRequest)
	if sent.GetSql() != "SELECT 1" || sent.GetMaxRows() != 5 {
		t.Fatalf("the analysis service got %v", sent)
	}
	if len(chunks) != 3 || chunks[0].GetColumns()[1].GetType() != "BIGINT" || !chunks[2].GetTruncated() {
		t.Fatalf("chunks: %v", chunks)
	}
	row := chunks[1].GetRows()[0].AsSlice()
	if row[0] != "probe.s1.v0.e1" || row[1] != 6.0 || row[2] != nil || len(row[3].([]any)) != 2 {
		t.Fatalf("row: %v", row)
	}

	refused, err := s.analysis.Query(t.Context(), bearer(token, &apiv1.QueryRequest{Sql: "SET x = 1"}))
	if err != nil {
		t.Fatal(err)
	}
	for refused.Receive() {
		t.Fatal("rows for a refused statement")
	}
	wantCode(t, refused.Err(), connect.CodeInvalidArgument)
	if !strings.Contains(refused.Err().Error(), "one SELECT") {
		t.Fatalf("the analysis service's message is lost: %v", refused.Err())
	}
}

func TestARuleScanCarriesTheCallerAndItsJobComesBackWhole(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	// A caller cannot name the actor: field 4 of the internal request, sent as an unknown
	// field of the public one, is dropped on the way.
	req := &apiv1.StartRuleScanRequest{RulesYaml: "rules: []", RunIds: []string{"r1"}, SubmissionIds: []string{"s1"}}
	req.ProtoReflect().SetUnknown([]byte{0x22, 0x03, 'e', 'v', 'e'})
	started, err := s.analysis.StartRuleScan(t.Context(), bearer(token, req))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.analysisFake.last().(*analysisv1.StartRuleScanRequest)
	if sent.GetActor() != "ada" || sent.GetRulesYaml() != "rules: []" || sent.GetRunIds()[0] != "r1" || sent.GetSubmissionIds()[0] != "s1" {
		t.Fatalf("the analysis service got %v", sent)
	}
	if job := started.Msg.GetJob(); job.GetCreatedBy() != "ada" || job.GetStatus() != "queued" || job.GetCreatedAt() == nil {
		t.Fatalf("StartRuleScan: %v", started.Msg)
	}

	got, err := s.analysis.GetJob(t.Context(), bearer(token, &apiv1.GetJobRequest{JobId: "j1"}))
	if err != nil {
		t.Fatal(err)
	}
	scan := got.Msg.GetJob().GetRuleScan()
	if scan.GetTotalMatches() != 1 || scan.GetRuns()[0].GetMatches() != 1 || scan.GetMatches()[0].GetVia()[0] != "base64" || scan.GetMatches()[0].GetSeq() != 6 {
		t.Fatalf("GetJob: %v", got.Msg)
	}
	_, err = s.analysis.GetJob(t.Context(), bearer(token, &apiv1.GetJobRequest{JobId: "nope"}))
	wantCode(t, err, connect.CodeNotFound)
}

func TestAReportComesBackWithEveryNumber(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	b := s.login(t, "ada", pw)

	res, err := s.analysis.Report(t.Context(), asBrowser(b, &apiv1.ReportRequest{SubmissionIds: []string{"s1"}, Suites: []string{"core.1"}, Compare: "m=x,y"}))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.analysisFake.last().(*analysisv1.ReportRequest)
	if sent.GetSubmissionIds()[0] != "s1" || sent.GetSuites()[0] != "core.1" || sent.GetCompare() != "m=x,y" {
		t.Fatalf("the analysis service got %v", sent)
	}
	r := res.Msg
	rate := r.GetRates()[0]
	if rate.GetScorer() != "leak" || rate.GetStderr() != 0.5 || rate.Stderr == nil || rate.GetCiHigh() != 0.9 || rate.GetVariantValuesJson() != `{"m":"x"}` {
		t.Fatalf("rate: %v", rate)
	}
	if r.GetUnscored()[0].GetRuns() != 3 || r.GetCoverage()[0].GetMissing() != 3 || r.GetForks()[0].GetValue() != 1 || r.GetDifferences()[0].GetDiff() != 0.25 || r.GetDifferences()[0].GetB().GetEpochs() != 2 || r.GetMarkdown() != "| leak |" {
		t.Fatalf("Report: %v", r)
	}
}

func TestAnExportIsRelayedInItsChunks(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	stream, err := s.analysis.DownloadExport(t.Context(), bearer(token, &apiv1.DownloadExportRequest{RunId: "r1", Format: apiv1.ExportFormat_EXPORT_FORMAT_PARQUET}))
	if err != nil {
		t.Fatal(err)
	}
	var got bytes.Buffer
	for stream.Receive() {
		got.Write(stream.Msg().GetChunk())
	}
	if err := stream.Err(); err != nil || got.String() != "first-second" {
		t.Fatalf("download: %q, %v", got.String(), err)
	}
	if sent := s.analysisFake.last().(*analysisv1.DownloadExportRequest); sent.GetFormat() != analysisv1.ExportFormat_EXPORT_FORMAT_PARQUET || sent.GetRunId() != "r1" {
		t.Fatalf("the analysis service got %v", sent)
	}
}

func TestAnalysisCallsNeedCredentialsAndAnAnalysisService(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.analysis.Report(t.Context(), connect.NewRequest(&apiv1.ReportRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	// The fake leaves Judge unimplemented, which is the analysis service's failure, not the
	// caller's.
	_, err = s.analysis.Judge(t.Context(), bearer(token, &apiv1.JudgeRequest{RunId: "r1", Question: "q", Model: "m"}))
	wantCode(t, err, connect.CodeUnavailable)

	// An edge started without --analysis.
	bare := httptest.NewServer(NewHandler(s.cfg, s.store, nil, nil, quietLog()))
	t.Cleanup(bare.Close)
	_, err = apiv1connect.NewAnalysisServiceClient(bare.Client(), bare.URL).Report(t.Context(), bearer(token, &apiv1.ReportRequest{}))
	wantCode(t, err, connect.CodeUnimplemented)
	if !strings.Contains(err.Error(), "--analysis") {
		t.Fatalf("the message does not say what to do: %v", err)
	}
}

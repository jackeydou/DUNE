package edge

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	analysisv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/analysis/v1/analysisv1connect"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// AnalysisService forwards to the analysis service. Its client is nil on a deployment that
// runs none, and every call then says so.
type AnalysisService struct {
	analysis analysisv1connect.AnalysisServiceClient
	log      *slog.Logger
}

// public copies an analysis service response into its public message. The public messages
// mirror the internal ones field for field, with the same numbers, so the copy goes through
// the wire format; TestAnalysisResponsesKeepEveryField holds the two in step. Requests are
// built by hand instead, so nothing a caller sends reaches a field edge fills in, such as the
// actor.
func public[T any, PT interface {
	*T
	proto.Message
}](src proto.Message) (*T, error) {
	wire, err := proto.Marshal(src)
	if err != nil {
		return nil, err
	}
	dst := PT(new(T))
	if err := proto.Unmarshal(wire, dst); err != nil {
		return nil, err
	}
	return (*T)(dst), nil
}

func (s *AnalysisService) client() (analysisv1connect.AnalysisServiceClient, error) {
	if s.analysis == nil {
		return nil, connect.NewError(connect.CodeUnimplemented, errors.New(
			"this deployment runs no analysis service: queries, reports, scans, the judge, traces, and export downloads are not available. The operator starts edge with --analysis to add them"))
	}
	return s.analysis, nil
}

func (s *AnalysisService) failed(op string, err error) error {
	var cerr *connect.Error
	if errors.As(err, &cerr) && passedThrough[cerr.Code()] {
		return connect.NewError(cerr.Code(), errors.New(cerr.Message()))
	}
	if errors.Is(err, context.Canceled) {
		return connect.NewError(connect.CodeCanceled, err)
	}
	s.log.Error("analysis service call failed", "op", op, "err", err)
	return connect.NewError(connect.CodeUnavailable, fmt.Errorf(
		"%s: the analysis service did not answer; try again, and tell the operator if it persists", op))
}

// answer maps an analysis service response, or its error, to the public one.
func answer[T any, PT interface {
	*T
	proto.Message
}, U any](s *AnalysisService, op string, res *connect.Response[U], err error) (*connect.Response[T], error) {
	if err != nil {
		return nil, s.failed(op, err)
	}
	out, err := public[T, PT](any(res.Msg).(proto.Message))
	if err != nil {
		s.log.Error("analysis service response does not convert", "op", op, "err", err)
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("%s: edge could not read the analysis service's answer; see the operator's log", op))
	}
	return connect.NewResponse(out), nil
}

func (s *AnalysisService) Query(ctx context.Context, req *connect.Request[apiv1.QueryRequest], out *connect.ServerStream[apiv1.QueryResponse]) error {
	client, err := s.client()
	if err != nil {
		return err
	}
	in, err := client.Query(ctx, connect.NewRequest(&analysisv1.QueryRequest{
		Sql: req.Msg.GetSql(), MaxRows: req.Msg.GetMaxRows(),
	}))
	if err != nil {
		return s.failed("Query", err)
	}
	defer func() { _ = in.Close() }() // the stream's outcome is in.Err(); Close only frees it
	for in.Receive() {
		chunk, err := public[apiv1.QueryResponse](in.Msg())
		if err != nil {
			s.log.Error("analysis service response does not convert", "op", "Query", "err", err)
			return connect.NewError(connect.CodeInternal, errors.New("Query: edge could not read the analysis service's answer; see the operator's log"))
		}
		if err := out.Send(chunk); err != nil {
			return err
		}
	}
	if err := in.Err(); err != nil {
		return s.failed("Query", err)
	}
	return nil
}

func (s *AnalysisService) SearchToolCalls(ctx context.Context, req *connect.Request[apiv1.SearchToolCallsRequest]) (*connect.Response[apiv1.SearchToolCallsResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.SearchToolCalls(ctx, connect.NewRequest(&analysisv1.SearchToolCallsRequest{
		RunIds:        req.Msg.GetRunIds(),
		SubmissionIds: req.Msg.GetSubmissionIds(),
		Tool:          req.Msg.GetTool(),
		AgentId:       req.Msg.GetAgentId(),
		FromTime:      req.Msg.GetFromTime(),
		ToTime:        req.Msg.GetToTime(),
		Limit:         req.Msg.GetLimit(),
	}))
	return answer[apiv1.SearchToolCallsResponse](s, "SearchToolCalls", res, err)
}

func (s *AnalysisService) StartRuleScan(ctx context.Context, req *connect.Request[apiv1.StartRuleScanRequest]) (*connect.Response[apiv1.StartRuleScanResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.StartRuleScan(ctx, connect.NewRequest(&analysisv1.StartRuleScanRequest{
		RulesYaml:     req.Msg.GetRulesYaml(),
		RunIds:        req.Msg.GetRunIds(),
		SubmissionIds: req.Msg.GetSubmissionIds(),
		Actor:         callerOf(ctx).user.Username,
	}))
	return answer[apiv1.StartRuleScanResponse](s, "StartRuleScan", res, err)
}

func (s *AnalysisService) GetJob(ctx context.Context, req *connect.Request[apiv1.GetJobRequest]) (*connect.Response[apiv1.GetJobResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.GetJob(ctx, connect.NewRequest(&analysisv1.GetJobRequest{JobId: req.Msg.GetJobId()}))
	return answer[apiv1.GetJobResponse](s, "GetJob", res, err)
}

func (s *AnalysisService) Judge(ctx context.Context, req *connect.Request[apiv1.JudgeRequest]) (*connect.Response[apiv1.JudgeResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.Judge(ctx, connect.NewRequest(&analysisv1.JudgeRequest{
		RunId:    req.Msg.GetRunId(),
		Question: req.Msg.GetQuestion(),
		Model:    req.Msg.GetModel(),
		FromSeq:  req.Msg.GetFromSeq(),
		ToSeq:    req.Msg.GetToSeq(),
	}))
	return answer[apiv1.JudgeResponse](s, "Judge", res, err)
}

func (s *AnalysisService) Report(ctx context.Context, req *connect.Request[apiv1.ReportRequest]) (*connect.Response[apiv1.ReportResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.Report(ctx, connect.NewRequest(&analysisv1.ReportRequest{
		SubmissionIds: req.Msg.GetSubmissionIds(),
		Suites:        req.Msg.GetSuites(),
		Compare:       req.Msg.GetCompare(),
	}))
	return answer[apiv1.ReportResponse](s, "Report", res, err)
}

func (s *AnalysisService) GetTrace(ctx context.Context, req *connect.Request[apiv1.GetTraceRequest]) (*connect.Response[apiv1.GetTraceResponse], error) {
	client, err := s.client()
	if err != nil {
		return nil, err
	}
	res, err := client.GetTrace(ctx, connect.NewRequest(&analysisv1.GetTraceRequest{
		RunId: req.Msg.GetRunId(), EventId: req.Msg.GetEventId(), EventChars: req.Msg.GetEventChars(),
	}))
	return answer[apiv1.GetTraceResponse](s, "GetTrace", res, err)
}

// DownloadExport relays the file's chunks as they arrive.
func (s *AnalysisService) DownloadExport(ctx context.Context, req *connect.Request[apiv1.DownloadExportRequest], out *connect.ServerStream[apiv1.DownloadExportResponse]) error {
	client, err := s.client()
	if err != nil {
		return err
	}
	in, err := client.DownloadExport(ctx, connect.NewRequest(&analysisv1.DownloadExportRequest{
		RunId: req.Msg.GetRunId(), Format: analysisv1.ExportFormat(req.Msg.GetFormat()),
	}))
	if err != nil {
		return s.failed("DownloadExport", err)
	}
	defer func() { _ = in.Close() }() // the stream's outcome is in.Err(); Close only frees it
	for in.Receive() {
		if err := out.Send(&apiv1.DownloadExportResponse{Chunk: in.Msg().GetChunk()}); err != nil {
			return err
		}
	}
	if err := in.Err(); err != nil {
		return s.failed("DownloadExport", err)
	}
	return nil
}

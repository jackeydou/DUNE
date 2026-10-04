package edge

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"connectrpc.com/connect"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
)

// RunService forwards to the orchestrator's Control API, naming the caller as the actor of each
// change.
type RunService struct {
	control controlv1connect.ControlServiceClient
	log     *slog.Logger
}

// passedThrough are Control API codes whose messages speak to the caller about their own
// request, so they go back as they are. Any other failure is the platform's, and the caller
// gets Unavailable while the operator's log gets the cause.
var passedThrough = map[connect.Code]bool{
	connect.CodeInvalidArgument:    true,
	connect.CodeNotFound:           true,
	connect.CodeFailedPrecondition: true,
	connect.CodeAlreadyExists:      true,
	connect.CodeAborted:            true,
	connect.CodeOutOfRange:         true,
	connect.CodeResourceExhausted:  true,
	connect.CodeCanceled:           true,
	connect.CodeDeadlineExceeded:   true,
}

func (s *RunService) upstream(op string, err error) error {
	var cerr *connect.Error
	if errors.As(err, &cerr) && passedThrough[cerr.Code()] {
		return connect.NewError(cerr.Code(), errors.New(cerr.Message()))
	}
	if errors.Is(err, context.Canceled) {
		return connect.NewError(connect.CodeCanceled, err)
	}
	s.log.Error("control plane call failed", "op", op, "err", err)
	return connect.NewError(connect.CodeUnavailable, fmt.Errorf(
		"%s: the control plane did not answer; try again, and tell the operator if it persists", op))
}

func (s *RunService) SubmitRuns(ctx context.Context, req *connect.Request[apiv1.SubmitRunsRequest]) (*connect.Response[apiv1.SubmitRunsResponse], error) {
	if n := len(req.Msg.GetCaseBundle()); n > MaxBundleBytes {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"the case bundle is %d bytes; the limit is %d (64 MiB). Move large data out of the case directory", n, MaxBundleBytes))
	}
	res, err := s.control.SubmitRuns(ctx, connect.NewRequest(&controlv1.SubmitRunsRequest{
		CaseBundle: req.Msg.GetCaseBundle(),
		Overrides:  req.Msg.GetOverrides(),
		Epochs:     req.Msg.GetEpochs(),
		Suite:      req.Msg.GetSuite(),
		Actor:      callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, s.upstream("SubmitRuns", err)
	}
	return connect.NewResponse(&apiv1.SubmitRunsResponse{
		SubmissionId: res.Msg.GetSubmissionId(),
		RunIds:       res.Msg.GetRunIds(),
	}), nil
}

func (s *RunService) SubmitSuite(ctx context.Context, req *connect.Request[apiv1.SubmitSuiteRequest]) (*connect.Response[apiv1.SubmitSuiteResponse], error) {
	total := len(req.Msg.GetSuiteYaml())
	for _, b := range req.Msg.GetCaseBundles() {
		total += len(b)
	}
	if total > MaxBundleBytes {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"the suite and its case bundles are %d bytes; the limit is %d (64 MiB) in all. Split the suite, or move large data out of the case directories", total, MaxBundleBytes))
	}
	res, err := s.control.SubmitSuite(ctx, connect.NewRequest(&controlv1.SubmitSuiteRequest{
		SuiteYaml:   req.Msg.GetSuiteYaml(),
		CaseBundles: req.Msg.GetCaseBundles(),
		Actor:       callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, s.upstream("SubmitSuite", err)
	}
	out := &apiv1.SubmitSuiteResponse{Suite: res.Msg.GetSuite()}
	for _, sub := range res.Msg.GetSubmissions() {
		out.Submissions = append(out.Submissions, &apiv1.SuiteSubmission{
			CaseId: sub.GetCaseId(), SubmissionId: sub.GetSubmissionId(), RunIds: sub.GetRunIds(),
		})
	}
	return connect.NewResponse(out), nil
}

func (s *RunService) GetRun(ctx context.Context, req *connect.Request[apiv1.GetRunRequest]) (*connect.Response[apiv1.GetRunResponse], error) {
	res, err := s.control.GetRun(ctx, connect.NewRequest(&controlv1.GetRunRequest{RunId: req.Msg.GetRunId()}))
	if err != nil {
		return nil, s.upstream("GetRun", err)
	}
	return connect.NewResponse(&apiv1.GetRunResponse{Run: runProto(res.Msg.GetRun())}), nil
}

func (s *RunService) ListRuns(ctx context.Context, req *connect.Request[apiv1.ListRunsRequest]) (*connect.Response[apiv1.ListRunsResponse], error) {
	res, err := s.control.ListRuns(ctx, connect.NewRequest(&controlv1.ListRunsRequest{
		SubmissionId: req.Msg.GetSubmissionId(),
		CaseId:       req.Msg.GetCaseId(),
		Status:       req.Msg.GetStatus(),
		Suite:        req.Msg.GetSuite(),
		Limit:        req.Msg.GetLimit(),
	}))
	if err != nil {
		return nil, s.upstream("ListRuns", err)
	}
	out := &apiv1.ListRunsResponse{Runs: make([]*apiv1.Run, len(res.Msg.GetRuns()))}
	for i, r := range res.Msg.GetRuns() {
		out.Runs[i] = runProto(r)
	}
	return connect.NewResponse(out), nil
}

func (s *RunService) CancelRun(ctx context.Context, req *connect.Request[apiv1.CancelRunRequest]) (*connect.Response[apiv1.CancelRunResponse], error) {
	res, err := s.control.CancelRun(ctx, connect.NewRequest(&controlv1.CancelRunRequest{
		RunId: req.Msg.GetRunId(),
		Actor: callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, s.upstream("CancelRun", err)
	}
	return connect.NewResponse(&apiv1.CancelRunResponse{Run: runProto(res.Msg.GetRun())}), nil
}

func (s *RunService) ResumeRun(ctx context.Context, req *connect.Request[apiv1.ResumeRunRequest]) (*connect.Response[apiv1.ResumeRunResponse], error) {
	res, err := s.control.ResumeRun(ctx, connect.NewRequest(&controlv1.ResumeRunRequest{
		RunId: req.Msg.GetRunId(),
		Actor: callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, s.upstream("ResumeRun", err)
	}
	return connect.NewResponse(&apiv1.ResumeRunResponse{Run: runProto(res.Msg.GetRun())}), nil
}

func (s *RunService) ForkRun(ctx context.Context, req *connect.Request[apiv1.ForkRunRequest]) (*connect.Response[apiv1.ForkRunResponse], error) {
	edits := make([]*controlv1.ForkEdit, len(req.Msg.GetEdits()))
	for i, e := range req.Msg.GetEdits() {
		edits[i] = forkEditProto(e)
	}
	res, err := s.control.ForkRun(ctx, connect.NewRequest(&controlv1.ForkRunRequest{
		RunId:     req.Msg.GetRunId(),
		AtEventId: req.Msg.GetAtEventId(),
		Edits:     edits,
		Actor:     callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, s.upstream("ForkRun", err)
	}
	return connect.NewResponse(&apiv1.ForkRunResponse{Run: runProto(res.Msg.GetRun())}), nil
}

// StreamEvents relays the Control API's stream as it arrives. It ends when the upstream stream
// ends or the caller goes away.
func (s *RunService) StreamEvents(ctx context.Context, req *connect.Request[apiv1.StreamEventsRequest], out *connect.ServerStream[apiv1.StreamEventsResponse]) error {
	in, err := s.control.StreamEvents(ctx, connect.NewRequest(&controlv1.StreamEventsRequest{
		RunId:    req.Msg.GetRunId(),
		AfterSeq: req.Msg.GetAfterSeq(),
	}))
	if err != nil {
		return s.upstream("StreamEvents", err)
	}
	defer func() { _ = in.Close() }() // the stream's outcome is in.Err(); Close only frees it
	for in.Receive() {
		e := in.Msg()
		if err := out.Send(&apiv1.StreamEventsResponse{
			Seq:         e.GetSeq(),
			EventId:     e.GetEventId(),
			Type:        e.GetType(),
			AgentId:     e.GetAgentId(),
			PayloadJson: e.GetPayloadJson(),
			Line:        e.GetLine(),
		}); err != nil {
			return err
		}
	}
	if err := in.Err(); err != nil {
		return s.upstream("StreamEvents", err)
	}
	return nil
}

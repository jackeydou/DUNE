//go:build integration

package edge

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/structpb"

	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
)

// fakeControl stands in for the orchestrator's Control API: it records each request and answers
// from a small in-memory table.
type fakeControl struct {
	controlv1connect.UnimplementedControlServiceHandler

	mu       sync.Mutex
	runs     map[string]*controlv1.Run
	requests []any
	// failWith, when set, is returned by every call.
	failWith error
	// events are what StreamEvents sends for any run, after holding the stream open for delay.
	events []*controlv1.StreamEventsResponse
	delay  time.Duration
}

func newFakeControl() *fakeControl {
	return &fakeControl{runs: map[string]*controlv1.Run{}}
}

func (f *fakeControl) record(msg any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, msg)
	return f.failWith
}

func (f *fakeControl) last() any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.requests[len(f.requests)-1]
}

func (f *fakeControl) fail(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failWith = err
}

func (f *fakeControl) run(id string) (*controlv1.Run, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.runs[id]
	if !ok {
		return nil, connect.NewError(connect.CodeNotFound, fmt.Errorf("no run `%s`.", id))
	}
	return r, nil
}

func (f *fakeControl) SubmitRuns(_ context.Context, req *connect.Request[controlv1.SubmitRunsRequest]) (*connect.Response[controlv1.SubmitRunsResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	if string(req.Msg.GetCaseBundle()) == "not a tar" {
		return nil, connect.NewError(connect.CodeInvalidArgument, errors.New("case bundle is not a tar archive: invalid header"))
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	id := "c.s1.v0.e1"
	f.runs[id] = &controlv1.Run{
		RunId: id, SubmissionId: "s1", CaseId: "c", Workspace: "safety", Status: "queued",
		TaskArgs: &structpb.Struct{Fields: map[string]*structpb.Value{"model": structpb.NewStringValue("m")}},
		Epoch:    1, Epochs: 1, OwnerId: "worker-7", SubmittedBy: req.Msg.GetActor(),
	}
	return connect.NewResponse(&controlv1.SubmitRunsResponse{SubmissionId: "s1", RunIds: []string{id}}), nil
}

func (f *fakeControl) GetRun(_ context.Context, req *connect.Request[controlv1.GetRunRequest]) (*connect.Response[controlv1.GetRunResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	r, err := f.run(req.Msg.GetRunId())
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&controlv1.GetRunResponse{Run: r}), nil
}

func (f *fakeControl) ListRuns(_ context.Context, req *connect.Request[controlv1.ListRunsRequest]) (*connect.Response[controlv1.ListRunsResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	res := &controlv1.ListRunsResponse{}
	for _, r := range f.runs {
		res.Runs = append(res.Runs, r)
	}
	return connect.NewResponse(res), nil
}

func (f *fakeControl) CancelRun(_ context.Context, req *connect.Request[controlv1.CancelRunRequest]) (*connect.Response[controlv1.CancelRunResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	r, err := f.run(req.Msg.GetRunId())
	if err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Status == "cancelled" {
		return nil, connect.NewError(connect.CodeFailedPrecondition, fmt.Errorf("run `%s` is already `cancelled`.", r.RunId))
	}
	r.Status, r.CancelledBy = "cancelled", req.Msg.GetActor()
	return connect.NewResponse(&controlv1.CancelRunResponse{Run: r}), nil
}

func (f *fakeControl) ResumeRun(_ context.Context, req *connect.Request[controlv1.ResumeRunRequest]) (*connect.Response[controlv1.ResumeRunResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	r, err := f.run(req.Msg.GetRunId())
	if err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	r.Status, r.ResumedBy = "running", req.Msg.GetActor()
	return connect.NewResponse(&controlv1.ResumeRunResponse{Run: r}), nil
}

func (f *fakeControl) ForkRun(_ context.Context, req *connect.Request[controlv1.ForkRunRequest]) (*connect.Response[controlv1.ForkRunResponse], error) {
	if err := f.record(req.Msg); err != nil {
		return nil, err
	}
	source, err := f.run(req.Msg.GetRunId())
	if err != nil {
		return nil, err
	}
	fork := &controlv1.Run{
		RunId: source.GetRunId() + ".f1", Status: "queued", ForkedFrom: source.GetRunId(), ForkSeq: 7,
		SubmittedBy: req.Msg.GetActor(),
	}
	return connect.NewResponse(&controlv1.ForkRunResponse{Run: fork}), nil
}

func (f *fakeControl) StreamEvents(_ context.Context, req *connect.Request[controlv1.StreamEventsRequest], out *connect.ServerStream[controlv1.StreamEventsResponse]) error {
	if err := f.record(req.Msg); err != nil {
		return err
	}
	if _, err := f.run(req.Msg.GetRunId()); err != nil {
		return err
	}
	time.Sleep(f.delay)
	for _, e := range f.events {
		if e.GetSeq() <= req.Msg.GetAfterSeq() {
			continue
		}
		if err := out.Send(e); err != nil {
			return err
		}
	}
	return nil
}

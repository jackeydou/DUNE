package edge

import (
	"context"
	"fmt"
	"log/slog"

	"connectrpc.com/connect"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
	"github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1/controlv1connect"
)

// CaseService forwards the case library to the orchestrator's Control API, naming the caller as
// the actor of each change.
type CaseService struct {
	control controlv1connect.ControlServiceClient
	log     *slog.Logger
}

func (s *CaseService) PushCase(ctx context.Context, req *connect.Request[apiv1.PushCaseRequest]) (*connect.Response[apiv1.PushCaseResponse], error) {
	if err := checkBundleSize(req.Msg.GetCaseBundle()); err != nil {
		return nil, err
	}
	res, err := s.control.PushCase(ctx, connect.NewRequest(&controlv1.PushCaseRequest{
		CaseBundle: req.Msg.GetCaseBundle(),
		Note:       req.Msg.GetNote(),
		Actor:      callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, upstream(s.log, "PushCase", err)
	}
	return connect.NewResponse(&apiv1.PushCaseResponse{
		Revision: caseRevisionProto(res.Msg.GetRevision()), Created: res.Msg.GetCreated(),
	}), nil
}

func (s *CaseService) UpdateCaseFiles(ctx context.Context, req *connect.Request[apiv1.UpdateCaseFilesRequest]) (*connect.Response[apiv1.UpdateCaseFilesResponse], error) {
	total := 0
	changes := make([]*controlv1.FileChange, len(req.Msg.GetChanges()))
	for i, c := range req.Msg.GetChanges() {
		total += len(c.GetContent())
		changes[i] = fileChangeProto(c)
	}
	if total > MaxBundleBytes {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf(
			"the changed files are %d bytes in all; the limit is %d (64 MiB)", total, MaxBundleBytes))
	}
	res, err := s.control.UpdateCaseFiles(ctx, connect.NewRequest(&controlv1.UpdateCaseFilesRequest{
		Workspace:    req.Msg.GetWorkspace(),
		CaseId:       req.Msg.GetCaseId(),
		BaseRevision: req.Msg.GetBaseRevision(),
		Changes:      changes,
		Note:         req.Msg.GetNote(),
		Actor:        callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, upstream(s.log, "UpdateCaseFiles", err)
	}
	return connect.NewResponse(&apiv1.UpdateCaseFilesResponse{
		Revision: caseRevisionProto(res.Msg.GetRevision()), Created: res.Msg.GetCreated(),
	}), nil
}

func (s *CaseService) GetCase(ctx context.Context, req *connect.Request[apiv1.GetCaseRequest]) (*connect.Response[apiv1.GetCaseResponse], error) {
	res, err := s.control.GetCase(ctx, connect.NewRequest(&controlv1.GetCaseRequest{
		Workspace: req.Msg.GetWorkspace(), CaseId: req.Msg.GetCaseId(),
	}))
	if err != nil {
		return nil, upstream(s.log, "GetCase", err)
	}
	return connect.NewResponse(&apiv1.GetCaseResponse{Case: caseProto(res.Msg.GetCase())}), nil
}

func (s *CaseService) ListCases(ctx context.Context, req *connect.Request[apiv1.ListCasesRequest]) (*connect.Response[apiv1.ListCasesResponse], error) {
	res, err := s.control.ListCases(ctx, connect.NewRequest(&controlv1.ListCasesRequest{
		Workspace: req.Msg.GetWorkspace(), IncludeArchived: req.Msg.GetIncludeArchived(),
	}))
	if err != nil {
		return nil, upstream(s.log, "ListCases", err)
	}
	out := &apiv1.ListCasesResponse{Cases: make([]*apiv1.Case, len(res.Msg.GetCases()))}
	for i, c := range res.Msg.GetCases() {
		out.Cases[i] = caseProto(c)
	}
	return connect.NewResponse(out), nil
}

func (s *CaseService) ListCaseRevisions(ctx context.Context, req *connect.Request[apiv1.ListCaseRevisionsRequest]) (*connect.Response[apiv1.ListCaseRevisionsResponse], error) {
	res, err := s.control.ListCaseRevisions(ctx, connect.NewRequest(&controlv1.ListCaseRevisionsRequest{
		Workspace: req.Msg.GetWorkspace(), CaseId: req.Msg.GetCaseId(),
	}))
	if err != nil {
		return nil, upstream(s.log, "ListCaseRevisions", err)
	}
	out := &apiv1.ListCaseRevisionsResponse{Revisions: make([]*apiv1.CaseRevision, len(res.Msg.GetRevisions()))}
	for i, r := range res.Msg.GetRevisions() {
		out.Revisions[i] = caseRevisionProto(r)
	}
	return connect.NewResponse(out), nil
}

func (s *CaseService) GetCaseRevision(ctx context.Context, req *connect.Request[apiv1.GetCaseRevisionRequest]) (*connect.Response[apiv1.GetCaseRevisionResponse], error) {
	res, err := s.control.GetCaseRevision(ctx, connect.NewRequest(&controlv1.GetCaseRevisionRequest{
		Workspace: req.Msg.GetWorkspace(), CaseId: req.Msg.GetCaseId(), Revision: req.Msg.GetRevision(),
	}))
	if err != nil {
		return nil, upstream(s.log, "GetCaseRevision", err)
	}
	out := &apiv1.GetCaseRevisionResponse{
		Revision: caseRevisionProto(res.Msg.GetRevision()),
		Files:    make([]*apiv1.CaseFile, len(res.Msg.GetFiles())),
	}
	for i, f := range res.Msg.GetFiles() {
		out.Files[i] = &apiv1.CaseFile{
			Path: f.GetPath(), Content: f.GetContent(), Mode: f.GetMode(), LinkTarget: f.GetLinkTarget(),
		}
	}
	return connect.NewResponse(out), nil
}

func (s *CaseService) ArchiveCase(ctx context.Context, req *connect.Request[apiv1.ArchiveCaseRequest]) (*connect.Response[apiv1.ArchiveCaseResponse], error) {
	res, err := s.control.ArchiveCase(ctx, connect.NewRequest(&controlv1.ArchiveCaseRequest{
		Workspace: req.Msg.GetWorkspace(), CaseId: req.Msg.GetCaseId(), Actor: callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, upstream(s.log, "ArchiveCase", err)
	}
	return connect.NewResponse(&apiv1.ArchiveCaseResponse{Case: caseProto(res.Msg.GetCase())}), nil
}

func (s *CaseService) UnarchiveCase(ctx context.Context, req *connect.Request[apiv1.UnarchiveCaseRequest]) (*connect.Response[apiv1.UnarchiveCaseResponse], error) {
	res, err := s.control.UnarchiveCase(ctx, connect.NewRequest(&controlv1.UnarchiveCaseRequest{
		Workspace: req.Msg.GetWorkspace(), CaseId: req.Msg.GetCaseId(), Actor: callerOf(ctx).user.Username,
	}))
	if err != nil {
		return nil, upstream(s.log, "UnarchiveCase", err)
	}
	return connect.NewResponse(&apiv1.UnarchiveCaseResponse{Case: caseProto(res.Msg.GetCase())}), nil
}

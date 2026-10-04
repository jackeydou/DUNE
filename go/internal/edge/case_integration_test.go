//go:build integration

package edge

import (
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
)

func TestCaseWritesCarryTheCallerAsActor(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)
	b := s.login(t, "ada", pw)

	pushed, err := s.cases.PushCase(t.Context(), bearer(token, &apiv1.PushCaseRequest{CaseBundle: []byte("tar"), Note: "first"}))
	if err != nil {
		t.Fatal(err)
	}
	push := s.control.last().(*controlv1.PushCaseRequest)
	if push.GetActor() != "ada" || push.GetNote() != "first" || string(push.GetCaseBundle()) != "tar" {
		t.Fatalf("Control API got %v", push)
	}
	if r := pushed.Msg.GetRevision(); !pushed.Msg.GetCreated() || r.GetRevision() != 3 || r.GetCreatedBy() != "ada" || r.GetBundleSha256() != "abc123" || r.GetCreatedAt() == nil {
		t.Fatalf("PushCase: %v", pushed.Msg)
	}

	edited, err := s.cases.UpdateCaseFiles(t.Context(), asBrowser(b, &apiv1.UpdateCaseFilesRequest{
		Workspace: "safety", CaseId: "demo", BaseRevision: 2, Note: "reword",
		Changes: []*apiv1.FileChange{
			{Path: "task.md", Change: &apiv1.FileChange_Content{Content: []byte("")}},
			{Path: "old.md", Change: &apiv1.FileChange_Delete{Delete: true}},
			{Path: "neither.md"},
		},
	}))
	if err != nil {
		t.Fatal(err)
	}
	edit := s.control.last().(*controlv1.UpdateCaseFilesRequest)
	if edit.GetActor() != "ada" || edit.GetWorkspace() != "safety" || edit.GetCaseId() != "demo" || edit.GetBaseRevision() != 2 || edit.GetNote() != "reword" {
		t.Fatalf("Control API got %v", edit)
	}
	changes := edit.GetChanges()
	// An empty file is still a write: the oneof keeps it apart from a change that sets nothing.
	if _, isWrite := changes[0].GetChange().(*controlv1.FileChange_Content); !isWrite || !changes[1].GetDelete() || changes[2].GetChange() != nil {
		t.Fatalf("changes: %v", changes)
	}
	if edited.Msg.GetRevision().GetRevision() != 3 {
		t.Fatalf("UpdateCaseFiles: %v", edited.Msg)
	}

	archived, err := s.cases.ArchiveCase(t.Context(), bearer(token, &apiv1.ArchiveCaseRequest{Workspace: "safety", CaseId: "demo"}))
	if err != nil {
		t.Fatal(err)
	}
	if s.control.last().(*controlv1.ArchiveCaseRequest).GetActor() != "ada" || archived.Msg.GetCase().GetArchivedAt() == nil {
		t.Fatalf("ArchiveCase: %v", archived.Msg)
	}
	restored, err := s.cases.UnarchiveCase(t.Context(), bearer(token, &apiv1.UnarchiveCaseRequest{Workspace: "safety", CaseId: "demo"}))
	if err != nil {
		t.Fatal(err)
	}
	if s.control.last().(*controlv1.UnarchiveCaseRequest).GetActor() != "ada" || restored.Msg.GetCase().GetArchivedAt() != nil {
		t.Fatalf("UnarchiveCase: %v", restored.Msg)
	}
}

func TestAnEditAgainstAnOldRevisionComesBackAborted(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.cases.UpdateCaseFiles(t.Context(), bearer(token, &apiv1.UpdateCaseFilesRequest{
		Workspace: "safety", CaseId: "demo", BaseRevision: 1,
		Changes: []*apiv1.FileChange{{Path: "task.md", Change: &apiv1.FileChange_Content{Content: []byte("x")}}},
	}))
	wantCode(t, err, connect.CodeAborted)
	if !strings.Contains(err.Error(), "is at revision 2") {
		t.Fatalf("the newest revision is not named: %v", err)
	}
}

func TestCasesAreReadThroughEdge(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	listed, err := s.cases.ListCases(t.Context(), bearer(token, &apiv1.ListCasesRequest{Workspace: "safety", IncludeArchived: true}))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.control.last().(*controlv1.ListCasesRequest)
	if !sent.GetIncludeArchived() || sent.GetWorkspace() != "safety" {
		t.Fatalf("Control API got %v", sent)
	}
	if c := listed.Msg.GetCases()[0]; c.GetCaseId() != "demo" || c.GetLatest().GetRevision() != 2 || c.GetLatest().GetCreatedBy() != "bob" || c.GetArchivedAt() != nil {
		t.Fatalf("ListCases: %v", listed.Msg)
	}

	got, err := s.cases.GetCase(t.Context(), bearer(token, &apiv1.GetCaseRequest{Workspace: "safety", CaseId: "demo"}))
	if err != nil || got.Msg.GetCase().GetWorkspace() != "safety" {
		t.Fatalf("GetCase: %v, %v", got, err)
	}
	_, err = s.cases.GetCase(t.Context(), bearer(token, &apiv1.GetCaseRequest{Workspace: "safety", CaseId: "nope"}))
	wantCode(t, err, connect.CodeNotFound)

	revisions, err := s.cases.ListCaseRevisions(t.Context(), bearer(token, &apiv1.ListCaseRevisionsRequest{Workspace: "safety", CaseId: "demo"}))
	if err != nil {
		t.Fatal(err)
	}
	if r := revisions.Msg.GetRevisions(); len(r) != 2 || r[1].GetNote() != "first" || r[1].GetCreatedBy() != "ada" {
		t.Fatalf("ListCaseRevisions: %v", revisions.Msg)
	}

	revision, err := s.cases.GetCaseRevision(t.Context(), bearer(token, &apiv1.GetCaseRevisionRequest{Workspace: "safety", CaseId: "demo", Revision: 2}))
	if err != nil {
		t.Fatal(err)
	}
	if s.control.last().(*controlv1.GetCaseRevisionRequest).GetRevision() != 2 {
		t.Fatalf("Control API got %v", s.control.last())
	}
	files := revision.Msg.GetFiles()
	if len(files) != 2 || files[0].GetLinkTarget() != "task.md" || string(files[1].GetContent()) != "Fix the bug." || files[1].GetMode() != 0o644 {
		t.Fatalf("GetCaseRevision: %v", revision.Msg)
	}
}

func TestRunsAreSubmittedByRevision(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	submitted, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{
		Case: &apiv1.CaseRevisionRef{Workspace: "safety", CaseId: "demo", Revision: 4},
	}))
	if err != nil {
		t.Fatal(err)
	}
	sent := s.control.last().(*controlv1.SubmitRunsRequest)
	if ref := sent.GetCase(); ref.GetWorkspace() != "safety" || ref.GetCaseId() != "demo" || ref.GetRevision() != 4 || len(sent.GetCaseBundle()) != 0 {
		t.Fatalf("Control API got %v", sent)
	}
	if submitted.Msg.GetCaseRevision() != 4 {
		t.Fatalf("SubmitRuns: %v", submitted.Msg)
	}
	run, err := s.runs.GetRun(t.Context(), bearer(token, &apiv1.GetRunRequest{RunId: submitted.Msg.GetRunIds()[0]}))
	if err != nil || run.Msg.GetRun().GetCaseRevision() != 4 {
		t.Fatalf("GetRun: %v, %v", run, err)
	}

	// A bundle submission sends no case reference at all, so the Control API sees one of the two.
	if _, err := s.runs.SubmitRuns(t.Context(), bearer(token, &apiv1.SubmitRunsRequest{CaseBundle: []byte("tar")})); err != nil {
		t.Fatal(err)
	}
	if s.control.last().(*controlv1.SubmitRunsRequest).GetCase() != nil {
		t.Fatal("a bundle submission carried a case reference")
	}
}

func TestCaseCallsNeedCredentialsAndRespectTheBundleLimit(t *testing.T) {
	s := newStack(t)
	s.user(t, "ada", pw, tenant.RoleMember)
	token := s.token(t, "ada", pw)

	_, err := s.cases.ListCases(t.Context(), connect.NewRequest(&apiv1.ListCasesRequest{}))
	wantCode(t, err, connect.CodeUnauthenticated)
	_, err = s.cases.PushCase(t.Context(), bearer(token, &apiv1.PushCaseRequest{CaseBundle: make([]byte, MaxBundleBytes+1)}))
	wantCode(t, err, connect.CodeInvalidArgument)
	if len(s.control.requests) != 0 {
		t.Fatal("a refused call reached the Control API")
	}
}

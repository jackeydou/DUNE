package edge

import (
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/jackeydou/DUNE/go/internal/edge/tenant"
	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
)

func roleProto(r tenant.Role) apiv1.Role {
	switch r {
	case tenant.RoleAdmin:
		return apiv1.Role_ROLE_ADMIN
	case tenant.RoleMember:
		return apiv1.Role_ROLE_MEMBER
	}
	return apiv1.Role_ROLE_UNSPECIFIED
}

func userProto(u tenant.User) *apiv1.User {
	return &apiv1.User{
		Username:  u.Username,
		Role:      roleProto(u.Role),
		Disabled:  u.Disabled,
		CreatedAt: timestamppb.New(u.CreatedAt),
	}
}

func optionalTime(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamppb.New(*t)
}

func tokenProto(t tenant.Token) *apiv1.ApiToken {
	return &apiv1.ApiToken{
		TokenId:    t.ID,
		Name:       t.Name,
		CreatedAt:  timestamppb.New(t.CreatedAt),
		ExpiresAt:  optionalTime(t.ExpiresAt),
		LastUsedAt: optionalTime(t.LastUsedAt),
		RevokedAt:  optionalTime(t.RevokedAt),
	}
}

// runProto maps the Control API's run to the public one. The owner, lease, and epoch fields
// are internal and stay behind.
func runProto(r *controlv1.Run) *apiv1.Run {
	return &apiv1.Run{
		RunId:         r.GetRunId(),
		SubmissionId:  r.GetSubmissionId(),
		CaseId:        r.GetCaseId(),
		Workspace:     r.GetWorkspace(),
		Status:        r.GetStatus(),
		Variant:       r.GetVariant(),
		VariantValues: r.GetTaskArgs(),
		Epoch:         r.GetEpoch(),
		Epochs:        r.GetEpochs(),
		CaseSha256:    r.GetCaseSha256(),
		CaseRevision:  r.GetCaseRevision(),
		Isolation:     r.GetIsolation(),
		Error:         r.GetError(),
		CreatedAt:     r.GetCreatedAt(),
		StartedAt:     r.GetStartedAt(),
		FinishedAt:    r.GetFinishedAt(),
		Replaces:      r.GetReplaces(),
		Suite:         r.GetSuite(),
		ForkedFrom:    r.GetForkedFrom(),
		ForkSeq:       r.GetForkSeq(),
		Fidelity:      r.GetFidelity(),
		Takeovers:     r.GetTakeovers(),
		SubmittedBy:   r.GetSubmittedBy(),
		CancelledBy:   r.GetCancelledBy(),
		ResumedBy:     r.GetResumedBy(),
	}
}

func forkEditProto(e *apiv1.ForkEdit) *controlv1.ForkEdit {
	switch edit := e.GetEdit().(type) {
	case *apiv1.ForkEdit_ReplaceMessage:
		m := edit.ReplaceMessage
		return &controlv1.ForkEdit{Edit: &controlv1.ForkEdit_ReplaceMessage{ReplaceMessage: &controlv1.ReplaceMessage{
			AgentId: m.GetAgentId(), Index: m.GetIndex(), Content: m.GetContent(),
		}}}
	case *apiv1.ForkEdit_DeleteMessage:
		m := edit.DeleteMessage
		return &controlv1.ForkEdit{Edit: &controlv1.ForkEdit_DeleteMessage{DeleteMessage: &controlv1.DeleteMessage{
			AgentId: m.GetAgentId(), Index: m.GetIndex(),
		}}}
	case *apiv1.ForkEdit_ReplaceDelivery:
		d := edit.ReplaceDelivery
		return &controlv1.ForkEdit{Edit: &controlv1.ForkEdit_ReplaceDelivery{ReplaceDelivery: &controlv1.ReplaceDelivery{
			SendEventId: d.GetSendEventId(), Recipient: d.GetRecipient(), Content: d.GetContent(),
		}}}
	case *apiv1.ForkEdit_ReplaceModel:
		m := edit.ReplaceModel
		return &controlv1.ForkEdit{Edit: &controlv1.ForkEdit_ReplaceModel{ReplaceModel: &controlv1.ReplaceModel{
			Slot: m.GetSlot(), Model: m.GetModel(),
		}}}
	}
	// An empty edit: the Control API refuses it as INVALID_ARGUMENT with its own message.
	return &controlv1.ForkEdit{}
}

func caseRevisionProto(r *controlv1.CaseRevision) *apiv1.CaseRevision {
	if r == nil {
		return nil
	}
	return &apiv1.CaseRevision{
		Workspace:    r.GetWorkspace(),
		CaseId:       r.GetCaseId(),
		Revision:     r.GetRevision(),
		BundleSha256: r.GetBundleSha256(),
		CreatedBy:    r.GetActor(),
		Note:         r.GetNote(),
		CreatedAt:    r.GetCreatedAt(),
	}
}

func caseProto(c *controlv1.Case) *apiv1.Case {
	if c == nil {
		return nil
	}
	return &apiv1.Case{
		Workspace:  c.GetWorkspace(),
		CaseId:     c.GetCaseId(),
		CreatedAt:  c.GetCreatedAt(),
		ArchivedAt: c.GetArchivedAt(),
		Latest:     caseRevisionProto(c.GetLatest()),
	}
}

func fileChangeProto(c *apiv1.FileChange) *controlv1.FileChange {
	out := &controlv1.FileChange{Path: c.GetPath()}
	switch change := c.GetChange().(type) {
	case *apiv1.FileChange_Content:
		out.Change = &controlv1.FileChange_Content{Content: change.Content}
	case *apiv1.FileChange_Delete:
		out.Change = &controlv1.FileChange_Delete{Delete: change.Delete}
	}
	// A change that sets neither: the Control API refuses it as INVALID_ARGUMENT with its own
	// message.
	return out
}

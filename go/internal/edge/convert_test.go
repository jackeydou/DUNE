package edge

import (
	"testing"

	controlv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/control/v1"
)

func TestRunKeepsRecoveryFieldsAndDropsTheOwner(t *testing.T) {
	run := runProto(&controlv1.Run{RunId: "r", OwnerId: "worker-1", Takeovers: 2, Fidelity: "fs_restored", Replaces: "r0"})
	if run.GetTakeovers() != 2 || run.GetFidelity() != "fs_restored" || run.GetReplaces() != "r0" {
		t.Fatalf("got %v", run)
	}
	// Which worker owns a run is the orchestrator's business: the public message has no
	// field for it.
	if run.ProtoReflect().Descriptor().Fields().ByName("owner_id") != nil {
		t.Fatal("the public Run has an owner_id field")
	}
}

package cli

import (
	"strings"
	"testing"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

func TestRunDetailsShowTakeoversAndFidelityOnlyWhenThereAreAny(t *testing.T) {
	show := func(r *apiv1.Run) string {
		t.Helper()
		var out strings.Builder
		if err := printRun(&out, r); err != nil {
			t.Fatal(err)
		}
		return out.String()
	}
	if plain := show(&apiv1.Run{RunId: "r", Status: "done"}); strings.Contains(plain, "taken over") || strings.Contains(plain, "fidelity") {
		t.Errorf("a run nobody took over:\n%s", plain)
	}
	once := show(&apiv1.Run{RunId: "r", Status: "interrupted", Takeovers: 1})
	if !strings.Contains(once, "taken over:    once, after its worker's lease ran out\n") {
		t.Errorf("one takeover:\n%s", once)
	}
	if twice := show(&apiv1.Run{RunId: "r", Takeovers: 2}); !strings.Contains(twice, "2 times, each after its worker's lease ran out") {
		t.Errorf("two takeovers:\n%s", twice)
	}
	fork := show(&apiv1.Run{RunId: "r.f1", ForkedFrom: "r", ForkSeq: 7, Fidelity: "fs_partial"})
	if !strings.Contains(fork, "r after seq 7 (fs_partial)") || strings.Contains(fork, "fidelity:") {
		t.Errorf("a fork's fidelity belongs to its `forked from` row:\n%s", fork)
	}
	if resumed := show(&apiv1.Run{RunId: "r", Fidelity: "fs_preserved"}); !strings.Contains(resumed, "fidelity:      fs_preserved\n") {
		t.Errorf("a fidelity that is not a fork's:\n%s", resumed)
	}
}

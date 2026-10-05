package cli

import (
	"bytes"
	"strings"
	"testing"
)

func TestViewPrintsTheRunsPage(t *testing.T) {
	t.Setenv(envConfig, t.TempDir()+"/config.yaml")
	t.Setenv(envEndpoint, "https://swarm.example.com/")
	t.Setenv(envToken, "")
	var out, errOut bytes.Buffer
	root := NewRoot(strings.NewReader(""), &out, &errOut)
	root.SetArgs([]string{"view", "demo.1a2b.v0.e1/../x", "--no-open"})
	if err := root.Execute(); err != nil {
		t.Fatal(err)
	}
	if got := out.String(); got != "https://swarm.example.com/runs/demo.1a2b.v0.e1%2F..%2Fx\n" {
		t.Fatalf("printed %q", got)
	}
}

//go:build integration

package sandboxd

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackeydou/DUNE/go/internal/driver"
	"github.com/jackeydou/DUNE/go/internal/driver/docker"
)

// displayImage is built from deploy/images/display, which needs the internet, so the test
// skips when the docker host does not have it.
func displayImage(t *testing.T, drv *docker.Driver) string {
	t.Helper()
	img := os.Getenv("SWARMEVAL_IT_DISPLAY_IMAGE")
	if img == "" {
		img = "swarmeval/display:dev"
	}
	rc, err := drv.ExportImagePath(context.Background(), img, "/etc/swarm-display", map[string]string{driver.LabelManaged: "true"})
	if err != nil {
		t.Skipf("display image %s is not usable (%v); build it with `docker build -f deploy/images/display/Dockerfile -t %s deploy/images/display`", img, err, img)
	}
	_ = rc.Close()
	return img
}

const page = `<html><head><title>Shop</title></head><body>
<label>Name <input></label><button onclick="document.title='paid'">Pay</button></body></html>`

func TestLiveDisplayDrivesTheBrowserAndKeepsItsProcessesOut(t *testing.T) {
	ctx := context.Background()
	drv, err := docker.New(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = drv.Close() })
	img := displayImage(t, drv)
	state, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig(state)
	if rt := os.Getenv("SWARMEVAL_IT_RUNTIME"); rt != "" {
		cfg.Runtime = rt
	}
	svc := New(cfg, drv, slog.New(slog.DiscardHandler))
	run := "it_display"
	t.Cleanup(func() {
		if err := svc.DestroyRun(context.Background(), run); err != nil {
			t.Errorf("destroy: %v", err)
		}
	})
	if err := svc.CreateRun(ctx, run, []string{"desk"}); err != nil {
		t.Fatal(err)
	}
	_, err = svc.CreateSandbox(ctx, CreateRequest{
		RunID: run, SandboxID: "desk", Image: img,
		Mounts:    []Mount{{Path: "/workspace"}, {Path: "/tmp"}},
		Resources: driver.Resources{MemoryBytes: 2 << 30, Pids: 512},
		Files:     []SeedFile{{Path: "/workspace/index.html", Content: []byte(page), Mode: 0o644}},
		Users:     []string{"agent"},
		Display:   &Display{Width: 1024, Height: 768, URL: "file:///workspace/index.html"},
	})
	if err != nil {
		t.Fatal(err)
	}
	shot := []string{DisplayDir + "/out/screenshot.png"}
	call := func(id, user string, argv []string, collect []string) ExecResult {
		t.Helper()
		res, err := svc.Exec(ctx, ExecRequest{RunID: run, SandboxID: "desk", CallID: id, Argv: argv, User: user, Timeout: time.Minute, Collect: collect})
		if err != nil {
			t.Fatal(err)
		}
		return res
	}
	png := func(res ExecResult) []byte {
		t.Helper()
		if len(res.Collected) != 1 || res.Collected[0].SHA256 == "" {
			t.Fatalf("collected = %+v; stderr %s", res.Collected, res.Stderr.Inline)
		}
		for _, b := range res.Blobs {
			if b.SHA256 == res.Collected[0].SHA256 {
				return b.Data
			}
		}
		t.Fatal("the collected screenshot is not among the blobs")
		return nil
	}

	res := call("call_1", DisplayUser, []string{"swarm-display", "browser", `{"action":"snapshot","screenshot":true}`}, shot)
	if res.ExitCode != 0 || !strings.Contains(string(res.Stdout.Inline), `button "Pay"`) {
		t.Fatalf("snapshot exit %d:\n%s\n%s", res.ExitCode, res.Stdout.Inline, res.Stderr.Inline)
	}
	if !bytes.HasPrefix(png(res), []byte("\x89PNG\r\n\x1a\n")) {
		t.Fatal("the browser screenshot is not a PNG")
	}
	ref := refOf(t, string(res.Stdout.Inline), `button "Pay"`)
	res = call("call_2", DisplayUser, []string{"swarm-display", "browser", `{"action":"click","ref":"` + ref + `"}`}, shot)
	if res.ExitCode != 0 || !strings.Contains(string(res.Stdout.Inline), "Title: paid") {
		t.Fatalf("click exit %d:\n%s\n%s", res.ExitCode, res.Stdout.Inline, res.Stderr.Inline)
	}
	if !res.Collected[0].Missing {
		t.Fatalf("a click without `screenshot` collected %+v", res.Collected)
	}
	res = call("call_3", DisplayUser, []string{"swarm-display", "computer", `{"action":"click","coordinate":[500,400]}`}, shot)
	if res.ExitCode != 0 || !bytes.HasPrefix(png(res), []byte("\x89PNG")) {
		t.Fatalf("computer click exit %d: %s", res.ExitCode, res.Stderr.Inline)
	}
	if len(res.Processes) != 0 || len(res.Changes) != 0 || len(res.Background) != 0 {
		t.Fatalf("display calls reported processes %+v, changes %+v, background %+v", res.Processes, res.Changes, res.Background)
	}

	res = call("call_4", "agent", []string{"sh", "-c", "DISPLAY=:1 xdotool getmouselocation; ls /run/swarm-display"}, nil)
	if res.ExitCode == 0 {
		t.Fatalf("the agent's user reached the display:\n%s", res.Stdout.Inline)
	}
	res = call("call_5", "agent", []string{"sh", "-c", "echo x > /workspace/note"}, nil)
	if len(res.Changes) != 1 || res.Changes[0].Ambiguous {
		t.Fatalf("changes = %+v; the browser's processes must not make the call's own write ambiguous", res.Changes)
	}
}

// refOf finds the ref of the snapshot line holding what.
func refOf(t *testing.T, snapshot, what string) string {
	t.Helper()
	for line := range strings.Lines(snapshot) {
		if i := strings.Index(line, "[ref="); strings.Contains(line, what) && i >= 0 {
			ref, _, _ := strings.Cut(line[i+len("[ref="):], "]")
			return ref
		}
	}
	t.Fatalf("no %s in the snapshot:\n%s", what, snapshot)
	return ""
}

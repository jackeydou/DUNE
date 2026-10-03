//go:build integration

package sandboxd

import (
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

// twoSandboxes creates sandboxes a and b of one run, each with its own identity, from image.
func twoSandboxes(t *testing.T, image string) (*Service, string) {
	t.Helper()
	ctx := context.Background()
	drv, err := docker.New(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = drv.Close() })
	state, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig(state)
	if rt := os.Getenv("SWARMEVAL_IT_RUNTIME"); rt != "" {
		cfg.Runtime = rt
	}
	svc := New(cfg, drv, slog.New(slog.DiscardHandler))
	run := "it_" + strings.ToLower(strings.ReplaceAll(t.Name(), "/", "_"))
	t.Cleanup(func() {
		if err := svc.DestroyRun(context.Background(), run); err != nil {
			t.Errorf("destroy: %v", err)
		}
	})
	if err := svc.CreateRun(ctx, run, []string{"a", "b"}); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"a", "b"} {
		token := strings.Repeat(id, 32)
		_, err := svc.CreateSandbox(ctx, CreateRequest{
			RunID: run, SandboxID: id, Image: image,
			Mounts:    []Mount{{Path: "/workspace"}},
			Resources: driver.Resources{MemoryBytes: 256 << 20, Pids: 128},
			Env:       map[string]string{"INSTANCE_ID": token},
			Hostname:  token,
			MachineID: token,
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	return svc, run
}

func shIn(t *testing.T, svc *Service, run, sandbox, call, script string, args ...string) ExecResult {
	t.Helper()
	res, err := svc.Exec(context.Background(), ExecRequest{
		RunID: run, SandboxID: sandbox, CallID: call, User: "0",
		Argv: append([]string{"sh", "-c", script, "sh"}, args...), Timeout: 30 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func TestLiveIdentityLandsInHostnameEnvAndMachineID(t *testing.T) {
	for _, image := range []string{"busybox:latest", "python:3.12-slim"} {
		t.Run(strings.Split(image, ":")[0], func(t *testing.T) {
			svc, run := twoSandboxes(t, image)

			res := shIn(t, svc, run, "a", "c1", `hostname; printf '%s\n' "$INSTANCE_ID" "$HOSTNAME"; cat /etc/machine-id`)

			token := strings.Repeat("a", 32)
			want := strings.Repeat(token+"\n", 4)
			if got := string(res.Stdout.Inline); got != want {
				t.Fatalf("stdout = %q, stderr = %q; want the token as hostname, INSTANCE_ID, HOSTNAME, and machine id", got, res.Stderr.Inline)
			}
			if len(res.Changes) != 0 {
				t.Fatalf("changes = %+v; the identity is part of the baseline", res.Changes)
			}
		})
	}
}

// The isolation probes the worker runs at run start, done by hand: what sandbox a plants, b must
// not see.
func TestLiveProbesFailBetweenTwoSandboxes(t *testing.T) {
	svc, run := twoSandboxes(t, "busybox:latest")
	const prefix, suffix = ".isolation-probe-", "it0123"
	marker := prefix + suffix

	planted := shIn(t, svc, run, "a", "probe:plant",
		`for d in /workspace /dev/shm; do : > "$d/$1"; done; sh -c 'sleep 60; :' "$1" >/dev/null 2>&1 &`, marker)
	if len(planted.Processes) == 0 {
		t.Fatalf("planted = %+v; the marker process must be running in a", planted)
	}

	// The marker is passed in two halves, so this script's own command line never holds it.
	check := shIn(t, svc, run, "b", "probe:check", `m=$1$2
for d in /workspace /dev/shm; do [ -e "$d/$m" ] && echo "FILE $d"; done
for d in /proc/[0-9]*; do
  case $(tr '\000' ' ' < "$d/cmdline" 2>/dev/null) in *"$m"*) echo "PROC $d" ;; esac
done
nslookup "$3" 2>&1 | grep -q '^Name:' && echo "DNS $3"
grep -v '^ *lo:' /proc/net/dev | grep ':' && echo "IFACE"
nc -w 2 1.1.1.1 80 </dev/null >/dev/null 2>&1 && echo "CONNECT"
echo checked`, prefix, suffix, strings.Repeat("a", 32))

	if got := string(check.Stdout.Inline); got != "checked\n" {
		t.Fatalf("b's check printed %q (stderr %q); a probe got through", got, check.Stderr.Inline)
	}

	// a sees its own marker, so the check above can see one when it is there.
	self := shIn(t, svc, run, "a", "probe:self", `m=$1$2; [ -e "/workspace/$m" ] && echo FILE
for d in /proc/[0-9]*; do
  case $(tr '\000' ' ' < "$d/cmdline" 2>/dev/null) in *"$m"*) echo PROC; break ;; esac
done`, prefix, suffix)
	if got := string(self.Stdout.Inline); got != "FILE\nPROC\n" {
		t.Fatalf("a's own check printed %q; the probe cannot see what it looks for", got)
	}
}

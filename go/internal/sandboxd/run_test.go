package sandboxd

import (
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

func newService(t *testing.T, drv *fakeDriver) *Service {
	t.Helper()
	return New(DefaultConfig(t.TempDir()), drv, slog.New(slog.DiscardHandler))
}

func TestCreateRunGivesEachSandboxItsOwnSubnetAroundTakenOnes(t *testing.T) {
	drv := &fakeDriver{subnets: []netip.Prefix{netip.MustParsePrefix("10.231.0.16/28"), netip.MustParsePrefix("172.17.0.0/16")}}
	svc := newService(t, drv)

	if err := svc.CreateRun(context.Background(), "run_1", []string{"dev", "qa"}); err != nil {
		t.Fatal(err)
	}

	if len(drv.networks) != 2 {
		t.Fatalf("networks = %+v", drv.networks)
	}
	dev, qa := drv.networks[0], drv.networks[1]
	if dev.Subnet.String() != "10.231.0.0/28" || qa.Subnet.String() != "10.231.0.32/28" {
		t.Fatalf("subnets = %s, %s; the taken 10.231.0.16/28 must be skipped", dev.Subnet, qa.Subnet)
	}
	if dev.Gateway.String() != "10.231.0.1" || dev.Name != "swarmeval-run_1-dev" {
		t.Fatalf("dev network = %+v", dev)
	}
	if dev.Labels[driver.LabelManaged] != "true" || qa.Labels[driver.LabelSandboxID] != "qa" {
		t.Fatalf("labels = %v, %v", dev.Labels, qa.Labels)
	}
}

func TestFreeSubnetReportsAnExhaustedPool(t *testing.T) {
	pool := netip.MustParsePrefix("10.0.0.0/27")
	used := []netip.Prefix{netip.MustParsePrefix("10.0.0.0/28"), netip.MustParsePrefix("10.0.0.16/28")}

	_, err := freeSubnet(pool, 28, used)

	if err == nil || !strings.Contains(err.Error(), "--sandbox-subnets") {
		t.Fatalf("err = %v; it must say how to get more room", err)
	}
}

func TestCreateRunFailingHalfwayRemovesWhatItCreated(t *testing.T) {
	drv := &fakeDriver{failNet: "swarmeval-run_1-qa"}
	svc := newService(t, drv)

	err := svc.CreateRun(context.Background(), "run_1", []string{"dev", "qa"})

	if err == nil || !slices.Equal(drv.netsGone, []driver.NetworkID{"n_swarmeval-run_1-dev"}) {
		t.Fatalf("err = %v, removed = %v", err, drv.netsGone)
	}
	if err := svc.CreateRun(context.Background(), "run_1", []string{"dev"}); err != nil {
		t.Fatalf("the run must be free to create again: %v", err)
	}
}

func TestCreateRunValidates(t *testing.T) {
	svc := newService(t, &fakeDriver{})
	ctx := context.Background()
	if err := svc.CreateRun(ctx, "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}

	for name, c := range map[string]struct {
		run       string
		sandboxes []string
		want      error
	}{
		"existing run":      {"run_1", []string{"box"}, ErrExists},
		"no sandboxes":      {"run_2", nil, ErrInvalid},
		"repeated sandbox":  {"run_2", []string{"a", "a"}, ErrInvalid},
		"bad sandbox id":    {"run_2", []string{"Box"}, ErrInvalid},
		"run id with slash": {"run/2", []string{"a"}, ErrInvalid},
	} {
		if err := svc.CreateRun(ctx, c.run, c.sandboxes); !errors.Is(err, c.want) {
			t.Errorf("%s: err = %v, want %v", name, err, c.want)
		}
	}
}

func TestCreateSandboxNeedsItsRun(t *testing.T) {
	svc := newService(t, &fakeDriver{})
	ctx := context.Background()

	_, err := svc.CreateSandbox(ctx, CreateRequest{RunID: "run_1", SandboxID: "box", Image: "img"})
	if !errors.Is(err, ErrNotFound) || !strings.Contains(err.Error(), "CreateRun") {
		t.Fatalf("before CreateRun: err = %v", err)
	}
	if err := svc.CreateRun(ctx, "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}
	_, err = svc.CreateSandbox(ctx, CreateRequest{RunID: "run_1", SandboxID: "other", Image: "img"})
	if !errors.Is(err, ErrInvalid) {
		t.Fatalf("unlisted sandbox: err = %v", err)
	}
}

func TestSandboxJoinsItsNetworkWithResolvConfAtTheGateway(t *testing.T) {
	f := newFixture(t)

	spec := f.drv.created[0]
	if spec.Network != "swarmeval-run_1-box" {
		t.Fatalf("network = %q", spec.Network)
	}
	resolv := spec.Binds[len(spec.Binds)-1]
	if resolv.ContainerPath != "/etc/resolv.conf" || !resolv.ReadOnly {
		t.Fatalf("resolv.conf bind = %+v", resolv)
	}
	if got, _ := os.ReadFile(resolv.HostPath); string(got) != "nameserver 10.231.0.1\n" {
		t.Fatalf("resolv.conf = %q", got)
	}
	if spec.DNS.String() != "10.231.0.1" {
		t.Fatalf("DNS = %v; docker's embedded resolver must forward only to the gateway", spec.DNS)
	}
	if strings.HasPrefix(resolv.HostPath, filepath.Join(f.state, "run_1", "box", "fs")) {
		t.Fatalf("resolv.conf at %s is under the key paths and would be diffed", resolv.HostPath)
	}
}

func TestDestroyRunRemovesNetworksAfterContainers(t *testing.T) {
	f := newFixture(t)

	if err := f.svc.DestroyRun(context.Background(), "run_1"); err != nil {
		t.Fatal(err)
	}

	if !slices.Equal(f.drv.netsGone, []driver.NetworkID{"n_swarmeval-run_1-box"}) || len(f.drv.removed) != 1 {
		t.Fatalf("containers removed = %v, networks removed = %v", f.drv.removed, f.drv.netsGone)
	}
	if err := f.svc.CreateRun(context.Background(), "run_1", []string{"box"}); err != nil {
		t.Fatalf("a destroyed run is forgotten: %v", err)
	}
}

func TestUsersAreAddedWithFreeIDsAndPrivateHomes(t *testing.T) {
	drv := &fakeDriver{etc: []tarEntry{
		{name: "etc/passwd", body: "root:x:0:0:root:/root:/bin/sh\napp:x:1000:1000::/app:/bin/sh"},
		{name: "etc/group", body: "root:x:0:\nstaff:x:1001:\n"},
	}}
	svc := newService(t, drv)
	if err := svc.CreateRun(context.Background(), "run_1", []string{"box"}); err != nil {
		t.Fatal(err)
	}

	_, err := svc.CreateSandbox(context.Background(), CreateRequest{
		RunID: "run_1", SandboxID: "box", Image: "img", Users: []string{"qa", "app", "dev"},
	})
	if err != nil {
		t.Fatal(err)
	}

	files := map[string]driver.File{}
	for _, f := range drv.created[0].Files {
		files[f.Path] = f
	}
	wantPasswd := "root:x:0:0:root:/root:/bin/sh\napp:x:1000:1000::/app:/bin/sh\n" +
		"dev:x:1002:1002::/home/dev:/bin/sh\nqa:x:1003:1003::/home/qa:/bin/sh\n"
	if got := string(files["/etc/passwd"].Content); got != wantPasswd {
		t.Fatalf("passwd =\n%s\nwant\n%s", got, wantPasswd)
	}
	if got := string(files["/etc/group"].Content); !strings.HasSuffix(got, "staff:x:1001:\ndev:x:1002:\nqa:x:1003:\n") {
		t.Fatalf("group =\n%s", got)
	}
	home := files["/home/qa"]
	if !home.Mode.IsDir() || home.Mode.Perm() != 0o700 || home.UID != 1003 || home.GID != 1003 {
		t.Fatalf("/home/qa = %+v", home)
	}
	if _, ok := files["/home/app"]; ok {
		t.Fatal("a user the image already has must be left alone")
	}
	if files["/home"].Mode != fs.ModeDir|0o755 {
		t.Fatalf("/home = %+v", files["/home"])
	}
}

func TestUsersNeedAPasswdFileAndValidNames(t *testing.T) {
	ctx := context.Background()
	for name, c := range map[string]struct {
		etc   []tarEntry
		users []string
	}{
		"no /etc":          {nil, []string{"qa"}},
		"no passwd":        {[]tarEntry{{name: "etc/group", body: "root:x:0:\n"}}, []string{"qa"}},
		"uppercase name":   {[]tarEntry{{name: "etc/passwd", body: "root:x:0:0::/:/bin/sh\n"}}, []string{"QA"}},
		"repeated name":    {[]tarEntry{{name: "etc/passwd", body: "root:x:0:0::/:/bin/sh\n"}}, []string{"qa", "qa"}},
		"name with colon":  {[]tarEntry{{name: "etc/passwd", body: "root:x:0:0::/:/bin/sh\n"}}, []string{"qa:0"}},
		"name with dotdot": {[]tarEntry{{name: "etc/passwd", body: "root:x:0:0::/:/bin/sh\n"}}, []string{".."}},
	} {
		drv := &fakeDriver{etc: c.etc}
		svc := newService(t, drv)
		if err := svc.CreateRun(ctx, "run_1", []string{"box"}); err != nil {
			t.Fatal(err)
		}
		_, err := svc.CreateSandbox(ctx, CreateRequest{RunID: "run_1", SandboxID: "box", Image: "img", Users: c.users})
		if !errors.Is(err, ErrInvalid) || len(drv.created) != 0 {
			t.Errorf("%s: err = %v, created = %d", name, err, len(drv.created))
		}
	}
}

package docker

import "testing"

func TestParseTopFindsColumnsByTitle(t *testing.T) {
	procs, err := parseTop(
		[]string{"PID", "PPID", "USER", "COMMAND"},
		[][]string{{"611", "592", "root", "/sbin/docker-init -- sleep infinity"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if p := procs[0]; p.PID != 611 || p.PPID != 592 || p.User != "root" || p.Cmdline != "/sbin/docker-init -- sleep infinity" {
		t.Fatalf("process = %+v", p)
	}
}

func TestParseTopAcceptsRunscColumns(t *testing.T) {
	procs, err := parseTop(
		[]string{"UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"},
		[][]string{{"0", "1", "0", "0", "10:00", "?", "00:00:00", "sleep"}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if p := procs[0]; p.PID != 1 || p.User != "0" || p.Cmdline != "sleep" {
		t.Fatalf("process = %+v", p)
	}
}

func TestParseTopRejectsMissingColumns(t *testing.T) {
	if _, err := parseTop([]string{"PID", "COMMAND"}, nil); err == nil {
		t.Fatal("want an error naming the missing columns")
	}
}

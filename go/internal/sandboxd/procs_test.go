package sandboxd

import (
	"slices"
	"testing"

	"github.com/jackeydou/DUNE/go/internal/driver"
)

func TestParseProcessesNamesUsersAndKeepsCommandLines(t *testing.T) {
	out := "U 0 root\nU 1001 qa\nU 1001 shadowed\n" +
		"P 1 0 0 /sbin/docker-init -- sleep infinity \n" +
		"P 14 1 1001 python3 -m http.server 8000 \n" +
		"P 20 14 4242 [kworker]\n"

	procs, err := parseProcesses(out)
	if err != nil {
		t.Fatal(err)
	}
	want := []driver.Process{
		{PID: 1, PPID: 0, User: "root", Cmdline: "/sbin/docker-init -- sleep infinity"},
		{PID: 14, PPID: 1, User: "qa", Cmdline: "python3 -m http.server 8000"},
		{PID: 20, PPID: 14, User: "4242", Cmdline: "[kworker]"},
	}
	if !slices.Equal(procs, want) {
		t.Fatalf("procs = %+v\nwant %+v", procs, want)
	}
}

func TestParseProcessesRefusesLinesItCannotRead(t *testing.T) {
	for _, out := range []string{
		"P 14 1 0\n",
		"P x 1 0 sh\n",
		"P 14 1 -1 sh\n",
		"sh -c rm -rf /\n",
	} {
		if _, err := parseProcesses(out); err == nil {
			t.Errorf("%q: parsed without error", out)
		}
	}
}

func TestParseProcessesSkipsUserLinesWithoutANumericID(t *testing.T) {
	procs, err := parseProcesses("U x evil\nU  \nP 3 1 0 sh\n")

	if err != nil || len(procs) != 1 || procs[0].User != "0" {
		t.Fatalf("procs = %+v, err = %v", procs, err)
	}
}

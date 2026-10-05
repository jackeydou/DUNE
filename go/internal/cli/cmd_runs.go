package cli

import (
	"errors"
	"fmt"
	"net/url"
	"os/exec"
	"runtime"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

func (a *app) runCommand() *cobra.Command {
	var (
		overrides []string
		epochs    int32
		suite     string
		library   string
		followRun bool
	)
	cmd := &cobra.Command{
		Use:   "run CASE_DIR | SUITE_FILE | --case WORKSPACE/CASE[@REVISION]",
		Short: "Submit a case directory, a suite file, or a case in the library",
		Long: "Submits a case directory, one run per variant and epoch, or a suite file, each case\n" +
			"its own submission under one suite label. A suite that does not load submits nothing.\n" +
			"A directory is stored in the case library as `swarm case push` stores it, and its runs\n" +
			"use that revision. --case runs a revision already there: the newest, or @REVISION.\n\n" +
			"-V replaces a variant axis's values: -V model=qwen3-8b,glm-5. Values are read as a\n" +
			"YAML flow sequence, so -V paraphrased=[],[dm_ab] gives two list values.",
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if (len(args) == 1) == (library != "") {
				return errors.New("give what to run as one of CASE_DIR, SUITE_FILE, and --case WORKSPACE/CASE[@REVISION]")
			}
			var ref *apiv1.CaseRevisionRef
			isSuite := false
			var err error
			if library != "" {
				ref, err = parseCaseRef(library)
			} else {
				isSuite, err = isSuiteFile(args[0])
			}
			if err != nil {
				return err
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			var runIDs []string
			if isSuite {
				if len(overrides) > 0 || epochs != 0 || suite != "" {
					return errors.New("-V, --epochs, and --suite apply to a case directory; a suite file sets its own variants and epochs")
				}
				text, bundles, err := suiteBundles(args[0])
				if err != nil {
					return err
				}
				res, err := c.runs.SubmitSuite(cmd.Context(), connect.NewRequest(&apiv1.SubmitSuiteRequest{
					SuiteYaml: text, CaseBundles: bundles,
				}))
				if err != nil {
					return explain("submit suite "+args[0], err)
				}
				for _, s := range res.Msg.GetSubmissions() {
					_, _ = fmt.Fprintf(a.out, "%s: submission %s, %d runs\n", s.GetCaseId(), s.GetSubmissionId(), len(s.GetRunIds()))
					runIDs = append(runIDs, s.GetRunIds()...)
				}
				_, _ = fmt.Fprintf(a.out, "suite %s: %d runs\n", res.Msg.GetSuite(), len(runIDs))
			} else {
				values, err := parseOverrides(overrides)
				if err != nil {
					return err
				}
				req, what := &apiv1.SubmitRunsRequest{Case: ref, Overrides: values, Epochs: epochs, Suite: suite}, library
				if ref == nil {
					what = args[0]
					if req.CaseBundle, err = pack(args[0]); err != nil {
						return err
					}
				}
				res, err := c.runs.SubmitRuns(cmd.Context(), connect.NewRequest(req))
				if err != nil {
					return explain("submit "+what, err)
				}
				runIDs = res.Msg.GetRunIds()
				_, _ = fmt.Fprintf(a.out, "submission %s: %d runs of revision %d\n", res.Msg.GetSubmissionId(), len(runIDs), res.Msg.GetCaseRevision())
			}
			if !followRun {
				for _, id := range runIDs {
					_, _ = fmt.Fprintln(a.out, id)
				}
				return nil
			}
			return follow(cmd.Context(), c, a.out, runIDs)
		},
	}
	cmd.Flags().StringArrayVarP(&overrides, "variant", "V", nil, "axis=values replacing a variant axis's values (repeatable)")
	cmd.Flags().Int32Var(&epochs, "epochs", 0, "runs per variant (default: the case's epochs)")
	cmd.Flags().StringVar(&suite, "suite", "", "label the submission as part of a suite run")
	cmd.Flags().StringVar(&library, "case", "", "run a case in the library, WORKSPACE/CASE[@REVISION], instead of a directory")
	cmd.Flags().BoolVarP(&followRun, "follow", "f", false, "print the runs' events as they happen, until every run has finished")
	return cmd
}

func (a *app) runsCommand() *cobra.Command {
	cmd := &cobra.Command{Use: "runs", Short: "List, inspect, cancel, and resume runs"}
	var filter apiv1.ListRunsRequest
	list := &cobra.Command{
		Use:   "list",
		Short: "List runs, newest first",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.runs.ListRuns(cmd.Context(), connect.NewRequest(&filter))
			if err != nil {
				return explain("list runs", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			return printRuns(a.out, res.Msg.GetRuns())
		},
	}
	list.Flags().StringVar(&filter.SubmissionId, "submission", "", "only this submission's runs")
	list.Flags().StringVar(&filter.CaseId, "case", "", "only this case's runs")
	list.Flags().StringVar(&filter.Status, "status", "", "only runs with this status")
	list.Flags().StringVar(&filter.Suite, "suite", "", "only this suite run's runs")
	list.Flags().StringVar(&filter.Workspace, "workspace", "", "only this workspace's runs")
	list.Flags().Int32Var(&filter.Limit, "limit", 100, "at most this many")

	get := &cobra.Command{
		Use:   "get RUN",
		Short: "Show one run",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.runs.GetRun(cmd.Context(), connect.NewRequest(&apiv1.GetRunRequest{RunId: args[0]}))
			if err != nil {
				return explain("get "+args[0], err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg.GetRun())
			}
			return printRun(a.out, res.Msg.GetRun())
		},
	}
	cancel := &cobra.Command{
		Use:   "cancel RUN...",
		Short: "Cancel runs: a queued one never starts, a running one stops at its next step",
		Args:  cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			for _, id := range args {
				res, err := c.runs.CancelRun(cmd.Context(), connect.NewRequest(&apiv1.CancelRunRequest{RunId: id}))
				if err != nil {
					return explain("cancel "+id, err)
				}
				_, _ = fmt.Fprintf(a.out, "%s %s\n", id, res.Msg.GetRun().GetStatus())
			}
			return nil
		},
	}
	resume := &cobra.Command{
		Use:   "resume RUN",
		Short: "Resume a run a Monitor paused",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.runs.ResumeRun(cmd.Context(), connect.NewRequest(&apiv1.ResumeRunRequest{RunId: args[0]}))
			if err != nil {
				return explain("resume "+args[0], err)
			}
			_, err = fmt.Fprintf(a.out, "%s %s\n", args[0], res.Msg.GetRun().GetStatus())
			return err
		},
	}
	cmd.AddCommand(list, get, cancel, resume)
	return cmd
}

func (a *app) eventsCommand() *cobra.Command {
	var after int64
	cmd := &cobra.Command{
		Use:   "events RUN",
		Short: "Print a run's events, one line each, as they happen until the run finishes",
		Long: "Prints `[event_id] #seq agent description`, the lines the LLM judge reads. For a run\n" +
			"still going it keeps printing until the run finishes; Ctrl-C stops it, not the run.\n" +
			"--json prints each event as JSON instead, with the full stored payload.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			stream, err := c.runs.StreamEvents(cmd.Context(), connect.NewRequest(&apiv1.StreamEventsRequest{
				RunId: args[0], AfterSeq: after,
			}))
			if err != nil {
				return explain("events of "+args[0], err)
			}
			defer func() { _ = stream.Close() }() // the outcome is stream.Err()
			for stream.Receive() {
				if a.asJSON {
					if err := printJSON(a.out, stream.Msg()); err != nil {
						return err
					}
					continue
				}
				if _, err := fmt.Fprintln(a.out, eventLine(stream.Msg())); err != nil {
					return err
				}
			}
			if err := stream.Err(); err != nil {
				return explain("events of "+args[0], err)
			}
			return nil
		},
	}
	cmd.Flags().Int64Var(&after, "after", 0, "start after this seq")
	return cmd
}

func (a *app) replayCommand() *cobra.Command {
	var at, editFile string
	var followRun bool
	cmd := &cobra.Command{
		Use:   "replay RUN --fork-at EVENT [--edit FILE]",
		Short: "Fork a finished run from the turn an event happened in, with edits",
		Long: "Queues a new run that goes on from RUN's state at the start of the turn EVENT\n" +
			"happened in. --edit names a YAML or JSON list of edits, each one of:\n\n" +
			"  - replace_message: {agent_id: a, index: 3, content: \"…\"}\n" +
			"  - delete_message: {agent_id: a, index: 4}\n" +
			"  - replace_delivery: {send_event_id: e17, recipient: b, content: \"…\"}",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if at == "" {
				return errors.New("--fork-at EVENT is required: the event whose turn the fork starts from")
			}
			var edits []*apiv1.ForkEdit
			if editFile != "" {
				var err error
				if edits, err = readEdits(editFile); err != nil {
					return err
				}
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.runs.ForkRun(cmd.Context(), connect.NewRequest(&apiv1.ForkRunRequest{
				RunId: args[0], AtEventId: at, Edits: edits,
			}))
			if err != nil {
				return explain("fork "+args[0], err)
			}
			fork := res.Msg.GetRun()
			_, _ = fmt.Fprintf(a.out, "%s: forked from %s after seq %d\n", fork.GetRunId(), args[0], fork.GetForkSeq())
			if followRun {
				return follow(cmd.Context(), c, a.out, []string{fork.GetRunId()})
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&at, "fork-at", "", "the event to fork at (required)")
	cmd.Flags().StringVar(&editFile, "edit", "", "a YAML or JSON file listing the edits")
	cmd.Flags().BoolVarP(&followRun, "follow", "f", false, "print the fork's events as they happen")
	return cmd
}

// opener is the command that opens a URL in the user's browser on this system.
func opener() (string, bool) {
	switch runtime.GOOS {
	case "darwin":
		return "open", true
	case "linux":
		return "xdg-open", true
	}
	return "", false
}

func (a *app) viewCommand() *cobra.Command {
	var noOpen bool
	cmd := &cobra.Command{
		Use:   "view RUN",
		Short: "Print the run's replay page in the console, and open it in a browser",
		Long: "Prints the address of the run's page in the console, which edge serves, and opens it\n" +
			"in your browser. The console asks you to sign in there: it does not use the CLI's token.\n" +
			"--no-open only prints.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, _, err := a.config()
			if err != nil {
				return err
			}
			base, err := checkEndpoint(cfg.Endpoint)
			if err != nil {
				return err
			}
			page := base + "/runs/" + url.PathEscape(args[0])
			if _, err := fmt.Fprintln(a.out, page); err != nil {
				return err
			}
			if noOpen {
				return nil
			}
			name, ok := opener()
			if !ok {
				return nil
			}
			// The address is printed either way, so a machine with no browser loses nothing.
			if err := exec.CommandContext(cmd.Context(), name, page).Start(); err != nil {
				_, _ = fmt.Fprintf(a.err, "swarm: could not open a browser (%v); open the address above\n", err)
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&noOpen, "no-open", false, "print the address without opening a browser")
	return cmd
}

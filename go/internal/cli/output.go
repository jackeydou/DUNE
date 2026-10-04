package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"text/tabwriter"
	"time"

	"connectrpc.com/connect"
	"golang.org/x/sync/errgroup"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// maxStreams bounds how many runs `--follow` streams at once; the rest wait their turn.
const maxStreams = 16

func printJSON(w io.Writer, m proto.Message) error {
	b, err := protojson.MarshalOptions{Multiline: true, EmitUnpopulated: false}.Marshal(m)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(w, string(b))
	return err
}

func when(t *timestamppb.Timestamp) string {
	if t == nil {
		return "-"
	}
	return t.AsTime().Local().Format(time.DateTime)
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func printRuns(w io.Writer, runs []*apiv1.Run) error {
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	_, _ = fmt.Fprintln(tw, "RUN\tSTATUS\tVARIANT\tEPOCH\tCREATED\tBY")
	for _, r := range runs {
		_, _ = fmt.Fprintf(tw, "%s\t%s\t%d\t%d/%d\t%s\t%s\n",
			r.GetRunId(), r.GetStatus(), r.GetVariant(), r.GetEpoch(), r.GetEpochs(), when(r.GetCreatedAt()), orDash(r.GetSubmittedBy()))
	}
	return tw.Flush()
}

func printRun(w io.Writer, r *apiv1.Run) error {
	values, err := protojson.Marshal(r.GetVariantValues())
	if err != nil {
		return err
	}
	rows := [][2]string{
		{"run", r.GetRunId()},
		{"status", r.GetStatus()},
		{"case", r.GetCaseId() + "@" + short(r.GetCaseSha256())},
		{"workspace", r.GetWorkspace()},
		{"submission", r.GetSubmissionId()},
		{"suite", orDash(r.GetSuite())},
		{"variant", fmt.Sprintf("%d %s", r.GetVariant(), values)},
		{"epoch", fmt.Sprintf("%d of %d", r.GetEpoch(), r.GetEpochs())},
		{"isolation", orDash(r.GetIsolation())},
		{"created", when(r.GetCreatedAt())},
		{"started", when(r.GetStartedAt())},
		{"finished", when(r.GetFinishedAt())},
		{"submitted by", orDash(r.GetSubmittedBy())},
	}
	if r.GetReplaces() != "" {
		rows = append(rows, [2]string{"reruns", r.GetReplaces()})
	}
	if r.GetForkedFrom() != "" {
		rows = append(rows, [2]string{"forked from", fmt.Sprintf("%s after seq %d (%s)", r.GetForkedFrom(), r.GetForkSeq(), orDash(r.GetFidelity()))})
	}
	if r.GetCancelledBy() != "" {
		rows = append(rows, [2]string{"cancelled by", r.GetCancelledBy()})
	}
	if r.GetResumedBy() != "" {
		rows = append(rows, [2]string{"resumed by", r.GetResumedBy()})
	}
	if r.GetError() != "" {
		rows = append(rows, [2]string{"error", r.GetError()})
	}
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	for _, row := range rows {
		_, _ = fmt.Fprintf(tw, "%s:\t%s\n", row[0], row[1])
	}
	return tw.Flush()
}

func short(sha string) string {
	if len(sha) > 8 {
		return sha[:8]
	}
	return sha
}

// eventLine is how the judge reads an event: `[event_id] #seq agent line`.
func eventLine(e *apiv1.StreamEventsResponse) string {
	return fmt.Sprintf("[%s] #%d %s %s", e.GetEventId(), e.GetSeq(), orDash(e.GetAgentId()), e.GetLine())
}

// follow streams the events of runs as they happen, each line prefixed with its run when there
// is more than one, until every run has finished. Then it prints each run's status, and fails
// when any run did not end `done`.
func follow(ctx context.Context, c *clients, w io.Writer, runIDs []string) error {
	var mu sync.Mutex
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(maxStreams)
	for _, id := range runIDs {
		g.Go(func() error {
			stream, err := c.runs.StreamEvents(gctx, connect.NewRequest(&apiv1.StreamEventsRequest{RunId: id}))
			if err != nil {
				return explain("follow "+id, err)
			}
			defer func() { _ = stream.Close() }() // the outcome is stream.Err()
			for stream.Receive() {
				line := eventLine(stream.Msg())
				if len(runIDs) > 1 {
					line = id + " " + line
				}
				mu.Lock()
				_, _ = fmt.Fprintln(w, line)
				mu.Unlock()
			}
			if err := stream.Err(); err != nil {
				return explain("follow "+id, err)
			}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		if ctx.Err() != nil {
			return errors.New("stopped following; the runs go on. Follow one again with `swarm events RUN`")
		}
		return err
	}
	var runs []*apiv1.Run
	var notDone []string
	for _, id := range runIDs {
		res, err := c.runs.GetRun(ctx, connect.NewRequest(&apiv1.GetRunRequest{RunId: id}))
		if err != nil {
			return explain("get "+id, err)
		}
		runs = append(runs, res.Msg.GetRun())
		if res.Msg.GetRun().GetStatus() != "done" {
			notDone = append(notDone, id)
		}
	}
	_, _ = fmt.Fprintln(w)
	if err := printRuns(w, runs); err != nil {
		return err
	}
	if len(notDone) > 0 {
		return fmt.Errorf("%d of %d runs did not end done: %s. `swarm runs get RUN` shows why", len(notDone), len(runIDs), strings.Join(notDone, ", "))
	}
	return nil
}

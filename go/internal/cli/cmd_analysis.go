package cli

import (
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// cell renders one query value for a table or CSV: text as it is, whole numbers without a
// fraction, lists and objects as JSON. null is the given text.
func cell(v any, null string) string {
	switch x := v.(type) {
	case nil:
		return null
	case string:
		return x
	case bool:
		return strconv.FormatBool(x)
	case float64:
		if x == math.Trunc(x) && math.Abs(x) < 1<<53 {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	}
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprint(v)
	}
	return string(b)
}

// oneLine keeps a table row on one line.
func oneLine(s string) string {
	return strings.NewReplacer("\n", `\n`, "\r", `\r`, "\t", `\t`).Replace(s)
}

func (a *app) queryCommand() *cobra.Command {
	var asCSV bool
	var maxRows int32
	cmd := &cobra.Command{
		Use:   "query SQL",
		Short: "Run one read-only SQL statement over exported runs",
		Long: "Runs one SELECT in DuckDB's SQL over two views: `runs`, one row per finished run\n" +
			"(submission, case, variant, status, scores), and `events`, the events of every exported\n" +
			"run (run_id, seq, event_id, ts, type, agent_id, parent_id, payload as JSON text).\n" +
			"The statement can read nothing else. At most 10,000 rows and 30 seconds.\n\n" +
			"  swarm query \"SELECT status, count(*) FROM runs GROUP BY ALL\"\n\n" +
			"Prints a table; --csv prints CSV, and --json one JSON object per row.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if asCSV && a.asJSON {
				return errors.New("--csv and --json are two formats; give one")
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			stream, err := c.analysis.Query(cmd.Context(), connect.NewRequest(&apiv1.QueryRequest{Sql: args[0], MaxRows: maxRows}))
			if err != nil {
				return explain("query", err)
			}
			defer func() { _ = stream.Close() }() // the outcome is stream.Err()
			var (
				columns   []string
				rows      int
				truncated bool
				tw        = tabwriter.NewWriter(a.out, 0, 0, 2, ' ', 0)
				cw        = csv.NewWriter(a.out)
			)
			for stream.Receive() {
				chunk := stream.Msg()
				if len(chunk.GetColumns()) > 0 {
					for _, col := range chunk.GetColumns() {
						columns = append(columns, col.GetName())
					}
					switch {
					case asCSV:
						if err := cw.Write(columns); err != nil {
							return err
						}
					case !a.asJSON:
						_, _ = fmt.Fprintln(tw, strings.Join(columns, "\t"))
					}
				}
				truncated = truncated || chunk.GetTruncated()
				for _, row := range chunk.GetRows() {
					rows++
					values := row.AsSlice()
					switch {
					case a.asJSON:
						record := make(map[string]any, len(values))
						for i, v := range values {
							record[columns[i]] = v
						}
						b, err := json.Marshal(record)
						if err != nil {
							return err
						}
						if _, err := fmt.Fprintln(a.out, string(b)); err != nil {
							return err
						}
					case asCSV:
						cells := make([]string, len(values))
						for i, v := range values {
							cells[i] = cell(v, "")
						}
						if err := cw.Write(cells); err != nil {
							return err
						}
					default:
						cells := make([]string, len(values))
						for i, v := range values {
							cells[i] = oneLine(cell(v, "NULL"))
						}
						_, _ = fmt.Fprintln(tw, strings.Join(cells, "\t"))
					}
				}
			}
			if err := stream.Err(); err != nil {
				return explain("query", err)
			}
			cw.Flush()
			if err := cw.Error(); err != nil {
				return err
			}
			if err := tw.Flush(); err != nil {
				return err
			}
			if truncated {
				_, _ = fmt.Fprintf(a.err, "swarm: the result was cut at %d rows; aggregate or filter in the statement, or raise --max-rows (at most 10000)\n", rows)
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&asCSV, "csv", false, "print CSV")
	cmd.Flags().Int32Var(&maxRows, "max-rows", 0, "rows to return at most (default and most: 10000)")
	return cmd
}

func (a *app) reportCommand() *cobra.Command {
	var req apiv1.ReportRequest
	cmd := &cobra.Command{
		Use:   "report [--submission ID]... [--suite LABEL]... [--compare AXIS=A,B]",
		Short: "Trigger rates per case revision, variant, and scorer",
		Long: "Prints, as Markdown, each scorer's rate over the `done` runs with a 95% interval, the\n" +
			"epochs each variant got, and the runs left out. Without --submission and --suite it\n" +
			"covers every run. --compare adds the difference in rate between two values of one\n" +
			"variant axis, the other axes equal: --compare 'paraphrased=[],[dm_ab]'.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.analysis.Report(cmd.Context(), connect.NewRequest(&req))
			if err != nil {
				return explain("report", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			_, err = fmt.Fprint(a.out, res.Msg.GetMarkdown())
			return err
		},
	}
	cmd.Flags().StringArrayVar(&req.SubmissionIds, "submission", nil, "only this submission's runs (repeatable)")
	cmd.Flags().StringArrayVar(&req.Suites, "suite", nil, "only this suite run's runs (repeatable; adds to --submission)")
	cmd.Flags().StringVar(&req.Compare, "compare", "", "AXIS=A,B: also the difference in rate between two values of an axis")
	return cmd
}

// exportFormats maps --format to the file it downloads and the name it gets by default.
var exportFormats = map[string]struct {
	format apiv1.ExportFormat
	suffix string
}{
	"eval":    {apiv1.ExportFormat_EXPORT_FORMAT_EVAL, ".eval"},
	"parquet": {apiv1.ExportFormat_EXPORT_FORMAT_PARQUET, ".events.parquet"},
}

func (a *app) exportCommand() *cobra.Command {
	var format, output string
	cmd := &cobra.Command{
		Use:   "export RUN [--format eval|parquet] [-o FILE]",
		Short: "Download a finished run's Inspect log or its events",
		Long: "Downloads the run's `.eval` (open it with `inspect view`) or its events as Parquet,\n" +
			"to RUN.eval or RUN.events.parquet in the current directory, or to -o FILE; `-o -`\n" +
			"writes to standard output. An existing file is not overwritten. Only runs that ended\n" +
			"`done` or `cancelled` are exported.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			kind, ok := exportFormats[format]
			if !ok {
				return fmt.Errorf("--format %q: want eval or parquet", format)
			}
			if output == "" {
				output = args[0] + kind.suffix
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			stream, err := c.analysis.DownloadExport(cmd.Context(), connect.NewRequest(&apiv1.DownloadExportRequest{RunId: args[0], Format: kind.format}))
			if err != nil {
				return explain("export "+args[0], err)
			}
			defer func() { _ = stream.Close() }() // the outcome is stream.Err()
			// The file is made on the first chunk, so a refused download leaves none behind.
			var (
				out   = a.out
				file  *os.File
				total int
			)
			for stream.Receive() {
				if file == nil && output != "-" {
					file, err = os.OpenFile(output, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
					if errors.Is(err, fs.ErrExist) {
						return fmt.Errorf("export %s: %s exists and is not overwritten. Remove it, or give another -o FILE", args[0], output)
					}
					if err != nil {
						return fmt.Errorf("export %s: %w", args[0], err)
					}
					out = file
				}
				n, err := out.Write(stream.Msg().GetChunk())
				total += n
				if err != nil {
					return fmt.Errorf("export %s: write %s: %w", args[0], output, err)
				}
			}
			err = stream.Err()
			if file != nil {
				if closeErr := file.Close(); err == nil && closeErr != nil {
					return fmt.Errorf("export %s: write %s: %w", args[0], output, closeErr)
				}
				if err != nil {
					// Half a file is worse than none.
					_ = os.Remove(output)
				}
			}
			if err != nil {
				return explain("export "+args[0], err)
			}
			if output != "-" {
				_, err = fmt.Fprintf(a.out, "%s: %d bytes\n", output, total)
			}
			return err
		},
	}
	cmd.Flags().StringVar(&format, "format", "eval", "eval (the Inspect log) or parquet (the events)")
	cmd.Flags().StringVarP(&output, "output", "o", "", "the file to write; - for standard output")
	return cmd
}

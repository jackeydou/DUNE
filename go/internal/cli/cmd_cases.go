package cli

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/tabwriter"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	apiv1 "github.com/jackeydou/DUNE/go/internal/gen/swarmeval/api/v1"
)

// parseCaseRef reads `WORKSPACE/CASE[@REVISION]`, how the CLI names a case in the library.
// Revision 0 is the newest.
func parseCaseRef(arg string) (*apiv1.CaseRevisionRef, error) {
	name, rev, pinned := strings.Cut(arg, "@")
	workspace, caseID, ok := strings.Cut(name, "/")
	if !ok || workspace == "" || caseID == "" || strings.Contains(caseID, "/") {
		return nil, fmt.Errorf("case %q: want WORKSPACE/CASE or WORKSPACE/CASE@REVISION, as `swarm case list` prints them", arg)
	}
	ref := &apiv1.CaseRevisionRef{Workspace: workspace, CaseId: caseID}
	if pinned {
		n, err := strconv.ParseInt(rev, 10, 32)
		if err != nil || n < 1 {
			return nil, fmt.Errorf("case %q: revision %q is not a number from 1 up", arg, rev)
		}
		ref.Revision = int32(n)
	}
	return ref, nil
}

// unpinnedCaseRef is parseCaseRef for commands that act on the case, not on one revision.
func unpinnedCaseRef(arg string) (*apiv1.CaseRevisionRef, error) {
	ref, err := parseCaseRef(arg)
	if err != nil {
		return nil, err
	}
	if ref.GetRevision() != 0 {
		return nil, fmt.Errorf("case %q: this command takes the case, WORKSPACE/CASE, without a revision", arg)
	}
	return ref, nil
}

func printRevisions(w io.Writer, revisions []*apiv1.CaseRevision) error {
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	_, _ = fmt.Fprintln(tw, "REVISION\tBUNDLE\tCREATED\tBY\tNOTE")
	for _, r := range revisions {
		_, _ = fmt.Fprintf(tw, "%d\t%s\t%s\t%s\t%s\n",
			r.GetRevision(), short(r.GetBundleSha256()), when(r.GetCreatedAt()), orDash(r.GetCreatedBy()), orDash(r.GetNote()))
	}
	return tw.Flush()
}

// writeCase writes a revision's files under dir, which must not exist or must be empty. Regular
// files go first and links last, and every path must stay inside dir, so nothing is written
// through a link.
func writeCase(dir string, files []*apiv1.CaseFile) error {
	entries, err := os.ReadDir(dir)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("pull into %s: %w", dir, err)
	}
	if len(entries) > 0 {
		return fmt.Errorf("pull into %s: the directory is not empty. Give an empty or a new directory", dir)
	}
	var links []*apiv1.CaseFile
	for _, f := range files {
		if !filepath.IsLocal(filepath.FromSlash(f.GetPath())) {
			return fmt.Errorf("pull into %s: the case holds a file at %q, which is not a path inside a directory; nothing more was written", dir, f.GetPath())
		}
		if f.GetLinkTarget() != "" {
			links = append(links, f)
			continue
		}
		path := filepath.Join(dir, filepath.FromSlash(f.GetPath()))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return fmt.Errorf("pull into %s: %w", dir, err)
		}
		if err := os.WriteFile(path, f.GetContent(), fs.FileMode(f.GetMode()).Perm()); err != nil {
			return fmt.Errorf("pull into %s: %w", dir, err)
		}
	}
	for _, f := range links {
		path := filepath.Join(dir, filepath.FromSlash(f.GetPath()))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return fmt.Errorf("pull into %s: %w", dir, err)
		}
		if err := os.Symlink(filepath.FromSlash(f.GetLinkTarget()), path); err != nil {
			return fmt.Errorf("pull into %s: %w", dir, err)
		}
	}
	return nil
}

func (a *app) caseCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "case",
		Short: "The case library: list, push, pull, and archive cases",
		Long: "Every case submitted or pushed is kept in the library with its revisions, numbered\n" +
			"from 1. Cases are named WORKSPACE/CASE, and one revision WORKSPACE/CASE@REVISION.",
	}

	var filter apiv1.ListCasesRequest
	list := &cobra.Command{
		Use:   "list",
		Short: "List cases, by workspace and id",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.cases.ListCases(cmd.Context(), connect.NewRequest(&filter))
			if err != nil {
				return explain("list cases", err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			tw := tabwriter.NewWriter(a.out, 0, 0, 2, ' ', 0)
			_, _ = fmt.Fprintln(tw, "CASE\tREVISION\tUPDATED\tBY\tARCHIVED")
			for _, cs := range res.Msg.GetCases() {
				archived := "-"
				if cs.GetArchivedAt() != nil {
					archived = when(cs.GetArchivedAt())
				}
				_, _ = fmt.Fprintf(tw, "%s/%s\t%d\t%s\t%s\t%s\n",
					cs.GetWorkspace(), cs.GetCaseId(), cs.GetLatest().GetRevision(),
					when(cs.GetLatest().GetCreatedAt()), orDash(cs.GetLatest().GetCreatedBy()), archived)
			}
			return tw.Flush()
		},
	}
	list.Flags().StringVar(&filter.Workspace, "workspace", "", "only this workspace's cases")
	list.Flags().BoolVar(&filter.IncludeArchived, "archived", false, "include archived cases")

	var note string
	push := &cobra.Command{
		Use:   "push CASE_DIR",
		Short: "Store a case directory as the newest revision of its case",
		Long: "Packs the directory and stores it as the next revision of the case its case.yaml\n" +
			"names, creating the case on its first push. A directory that is the newest revision\n" +
			"already makes no new one. Nothing is run: `swarm run` submits.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			bundle, err := pack(args[0])
			if err != nil {
				return err
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.cases.PushCase(cmd.Context(), connect.NewRequest(&apiv1.PushCaseRequest{CaseBundle: bundle, Note: note}))
			if err != nil {
				return explain("push "+args[0], err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			r := res.Msg.GetRevision()
			state := "pushed"
			if !res.Msg.GetCreated() {
				state = "unchanged, already"
			}
			_, err = fmt.Fprintf(a.out, "%s/%s@%d %s\n", r.GetWorkspace(), r.GetCaseId(), r.GetRevision(), state)
			return err
		},
	}
	push.Flags().StringVarP(&note, "note", "m", "", "what changed, for the revision list")

	pull := &cobra.Command{
		Use:   "pull WORKSPACE/CASE[@REVISION] [DIR]",
		Short: "Write a revision's files to a directory",
		Long: "Writes the newest revision, or the one named, to DIR (default: ./CASE). DIR must be\n" +
			"empty or not exist yet.",
		Args: cobra.RangeArgs(1, 2),
		RunE: func(cmd *cobra.Command, args []string) error {
			ref, err := parseCaseRef(args[0])
			if err != nil {
				return err
			}
			dir := ref.GetCaseId()
			if len(args) == 2 {
				dir = args[1]
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.cases.GetCaseRevision(cmd.Context(), connect.NewRequest(&apiv1.GetCaseRevisionRequest{
				Workspace: ref.GetWorkspace(), CaseId: ref.GetCaseId(), Revision: ref.GetRevision(),
			}))
			if err != nil {
				return explain("pull "+args[0], err)
			}
			if err := writeCase(dir, res.Msg.GetFiles()); err != nil {
				return err
			}
			r := res.Msg.GetRevision()
			_, err = fmt.Fprintf(a.out, "%s/%s@%d: %d files in %s\n", r.GetWorkspace(), r.GetCaseId(), r.GetRevision(), len(res.Msg.GetFiles()), dir)
			return err
		},
	}

	revisions := &cobra.Command{
		Use:   "revisions WORKSPACE/CASE",
		Short: "List a case's revisions, newest first",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ref, err := unpinnedCaseRef(args[0])
			if err != nil {
				return err
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			res, err := c.cases.ListCaseRevisions(cmd.Context(), connect.NewRequest(&apiv1.ListCaseRevisionsRequest{
				Workspace: ref.GetWorkspace(), CaseId: ref.GetCaseId(),
			}))
			if err != nil {
				return explain("revisions of "+args[0], err)
			}
			if a.asJSON {
				return printJSON(a.out, res.Msg)
			}
			return printRevisions(a.out, res.Msg.GetRevisions())
		},
	}

	archive := &cobra.Command{
		Use:   "archive WORKSPACE/CASE",
		Short: "Archive a case: it leaves the list and takes no pushes, edits, or runs",
		Long: "Archiving deletes nothing: the case's revisions stay, because runs reference them,\n" +
			"and `swarm case unarchive` takes it back.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ref, err := unpinnedCaseRef(args[0])
			if err != nil {
				return err
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			if _, err := c.cases.ArchiveCase(cmd.Context(), connect.NewRequest(&apiv1.ArchiveCaseRequest{
				Workspace: ref.GetWorkspace(), CaseId: ref.GetCaseId(),
			})); err != nil {
				return explain("archive "+args[0], err)
			}
			_, err = fmt.Fprintf(a.out, "%s archived\n", args[0])
			return err
		},
	}
	unarchive := &cobra.Command{
		Use:   "unarchive WORKSPACE/CASE",
		Short: "Take an archived case back",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ref, err := unpinnedCaseRef(args[0])
			if err != nil {
				return err
			}
			c, err := a.signedIn()
			if err != nil {
				return err
			}
			if _, err := c.cases.UnarchiveCase(cmd.Context(), connect.NewRequest(&apiv1.UnarchiveCaseRequest{
				Workspace: ref.GetWorkspace(), CaseId: ref.GetCaseId(),
			})); err != nil {
				return explain("unarchive "+args[0], err)
			}
			_, err = fmt.Fprintf(a.out, "%s unarchived\n", args[0])
			return err
		},
	}
	cmd.AddCommand(list, push, pull, revisions, archive, unarchive)
	return cmd
}

# Changelog

## [Unreleased]

### Added
- The case flow draws no sandbox for an agent with `sandbox: none` (case version 5), and its
  details say `none`. New case's template is `schema_version: 5`.
- A case's Files tab opens on a flow: `case.yaml` and its env file, pending edits included,
  drawn as variant axes, the task, agents, channels, extensions, sandboxes, profiles, canaries,
  and scorers, with what connects them. A click shows a part's settings, its connections, its
  lines of YAML, and the files it names; "Edit" opens those lines in the source view. The view
  is in the URL (`?view=source`).

### Changed
- The file editor: a file tree with folders and icons, the path and actions above the editor,
  a status bar (language, cursor, lines, saved state), wrapping on a toggle, and syntax colours
  from the theme.
- A sidebar replaces the top bar: pages grouped under Evaluate, Investigate, and Admin, the
  account menu and sign-out at its foot, collapsible to icons (`Ctrl/⌘ B`). Each page has
  breadcrumbs, a title with one line about the page, and its actions.
- tweakcn's "Claude" theme in place of shadcn's neutral one, with system fonts instead of
  Geist and serif page titles. Run and job states are coloured pills.
- Runs: counts by status that filter on a click, one-line filters, each submission's case
  linked with its runs' states, variant values as chips, durations and relative times, and
  trigger rates drawn as bars with their interval.
- Run: an overview card and score cards side by side; the replay in a panel of fixed height
  with the event's detail beside it, events tagged by type, and identifiers that copy.
- Cases, a case, Analysis, Account, and Users: tables on cards, underlined tabs, a two-pane file
  editor themed to match, and upload as a button.
- A case's Run tab chooses the models: one or more per model slot of the revision, from what
  model-gateway serves (`spec/2026-10-06-run-time-models`). A revision that no longer loads says
  why instead of offering to run. The new-case template is `case.yaml` schema version 4, with no
  model.

### Added
- The fork dialog runs a model slot on another model from the fork point.
- A run's page lists its model per slot.
- The console: sign-in; runs grouped by submission with filters and each submission's trigger
  rates; a run's status, scores, cancel, and resume; the replay, one lane per agent, live while
  the run goes on, with type filters, each event's stored payload and causal chain, and forking
  from an event; two runs side by side; the case library with a file editor, revision history,
  diffs between revisions, new cases from a template, submitting a revision, and archiving;
  analysis (SQL, tool call search, rule scans, the judge); account (password, API tokens); and
  user management for admins. Built with Vite, React, TanStack Router and Query, shadcn/ui,
  and CodeMirror 6, on a Connect-ES client generated from the public API.

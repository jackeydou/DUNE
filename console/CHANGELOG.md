# Changelog

## [Unreleased]

### Changed
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

### Added
- The console: sign-in; runs grouped by submission with filters and each submission's trigger
  rates; a run's status, scores, cancel, and resume; the replay, one lane per agent, live while
  the run goes on, with type filters, each event's stored payload and causal chain, and forking
  from an event; two runs side by side; the case library with a file editor, revision history,
  diffs between revisions, new cases from a template, submitting a revision, and archiving;
  analysis (SQL, tool call search, rule scans, the judge); account (password, API tokens); and
  user management for admins. Built with Vite, React, TanStack Router and Query, shadcn/ui,
  and CodeMirror 6, on a Connect-ES client generated from the public API.

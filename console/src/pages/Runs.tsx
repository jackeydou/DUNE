import { useQuery } from "@tanstack/react-query"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { ChartNoAxesColumn, X } from "lucide-react"
import { useMemo, useState } from "react"

import { analysis, cases, runs, when } from "@/api"
import {
  ErrorAlert,
  Loading,
  Page,
  StatusBadge,
  StatusCounts,
  VariantChips,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { Run } from "@/gen/swarmeval/api/v1/run_pb"
import { ago, duration } from "@/lib/time"
import { cn } from "@/lib/utils"

const STATUSES = [
  "queued",
  "running",
  "paused",
  "interrupted",
  "done",
  "failed",
  "cancelled",
]

export interface RunsSearch {
  workspace?: string
  case?: string
  suite?: string
  status?: string
  submission?: string
}

function pct(x: number): string {
  return `${(x * 100).toFixed(0)}%`
}

/** One submission's trigger rates, asked for when the reader opens them. */
function SubmissionRates({ submissionId }: { submissionId: string }) {
  const report = useQuery({
    queryKey: ["report", submissionId],
    queryFn: () => analysis.report({ submissionIds: [submissionId] }),
    retry: false,
  })
  if (report.isPending)
    return (
      <div className="px-4 py-3">
        <Loading what="rates" />
      </div>
    )
  if (report.error)
    return (
      <div className="px-4 py-3">
        <ErrorAlert title="The rates did not load" error={report.error} />
      </div>
    )
  if (report.data.rates.length === 0)
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">
        No run of this submission has ended done and been scored yet.
      </p>
    )
  return (
    <Table data-testid="rates">
      <TableHeader>
        <TableRow>
          <TableHead className="pl-4">Variant</TableHead>
          <TableHead>Scorer</TableHead>
          <TableHead className="text-right">Epochs</TableHead>
          <TableHead className="w-64">Rate</TableHead>
          <TableHead>95% interval</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {report.data.rates.map((r) => (
          <TableRow key={`${r.variant}.${r.scorer}`}>
            <TableCell className="pl-4">
              <VariantChips values={JSON.parse(r.variantValuesJson || "{}")} />
            </TableCell>
            <TableCell className="font-medium">{r.scorer}</TableCell>
            <TableCell className="text-right tabular-nums">
              {r.epochs}
            </TableCell>
            <TableCell>
              <div className="flex items-center gap-2">
                <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className="absolute inset-y-0 bg-primary/25"
                    style={{
                      left: pct(r.ciLow),
                      width: pct(r.ciHigh - r.ciLow),
                    }}
                  />
                  <div
                    className="absolute inset-y-0 left-0 bg-primary"
                    style={{ width: pct(r.rate) }}
                  />
                </div>
                <span className="w-10 text-right font-medium tabular-nums">
                  {pct(r.rate)}
                </span>
              </div>
            </TableCell>
            <TableCell className="text-muted-foreground tabular-nums">
              {pct(r.ciLow)} – {pct(r.ciHigh)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Submission({ id, items }: { id: string; items: Run[] }) {
  const [rates, setRates] = useState(false)
  const first = items[0]
  return (
    <section
      className="overflow-hidden rounded-xl border bg-card shadow-xs"
      data-testid="submission"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to="/cases/$workspace/$caseId"
              params={{ workspace: first.workspace, caseId: first.caseId }}
              className="font-mono text-sm font-medium underline-offset-4 hover:underline"
            >
              {first.workspace}/{first.caseId}@{first.caseRevision}
            </Link>
            <StatusCounts statuses={items.map((r) => r.status)} />
          </div>
          <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
            <span>
              submission{" "}
              <Link
                to="/runs"
                search={{ submission: id }}
                className="font-mono hover:text-foreground hover:underline"
              >
                {id}
              </Link>
            </span>
            {first.suite && <span>suite {first.suite}</span>}
            <span title={when(first.createdAt)}>{ago(first.createdAt)}</span>
            {first.submittedBy && <span>by {first.submittedBy}</span>}
            <span>
              {items.length} run{items.length === 1 ? "" : "s"}
            </span>
          </div>
        </div>
        <Button
          variant={rates ? "secondary" : "outline"}
          size="sm"
          onClick={() => setRates(!rates)}
        >
          <ChartNoAxesColumn /> {rates ? "Hide rates" : "Show rates"}
        </Button>
      </div>
      {rates && (
        <div className="border-t bg-muted/30">
          <SubmissionRates submissionId={id} />
        </div>
      )}
      <Table className="border-t">
        <TableHeader className="bg-muted/40">
          <TableRow>
            <TableHead className="pl-4">Run</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Variant</TableHead>
            <TableHead className="text-right">Epoch</TableHead>
            <TableHead className="text-right">Duration</TableHead>
            <TableHead className="pr-4 text-right">Finished</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((r) => (
            <TableRow key={r.runId}>
              <TableCell className="pl-4">
                <Link
                  to="/runs/$runId"
                  params={{ runId: r.runId }}
                  data-testid="run-link"
                  className="font-mono text-xs text-primary underline-offset-4 hover:underline"
                >
                  {r.runId}
                </Link>
              </TableCell>
              <TableCell>
                <StatusBadge status={r.status} />
              </TableCell>
              <TableCell>
                <VariantChips values={r.variantValues} />
              </TableCell>
              <TableCell className="text-right text-muted-foreground tabular-nums">
                {r.epoch}/{r.epochs}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {duration(r.startedAt, r.finishedAt)}
              </TableCell>
              <TableCell
                className="pr-4 text-right text-muted-foreground"
                title={when(r.finishedAt)}
              >
                {ago(r.finishedAt)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  )
}

/** The runs listed, counted by state. A state's tile filters to it; the chosen one clears it. */
function Summary({
  all,
  status,
  onStatus,
}: {
  all: Run[]
  status?: string
  onStatus: (s?: string) => void
}) {
  const counts = new Map<string, number>()
  for (const r of all) counts.set(r.status, (counts.get(r.status) ?? 0) + 1)
  const tiles: [string | undefined, number][] = [
    [undefined, all.length],
    ...STATUSES.filter((s) => counts.has(s) || s === status).map(
      (s): [string, number] => [s, counts.get(s) ?? 0]
    ),
  ]
  return (
    <div className="flex flex-wrap gap-2">
      {tiles.map(([value, n]) => (
        <button
          key={value ?? ""}
          type="button"
          onClick={() => onStatus(value === status ? undefined : value)}
          className={cn(
            "flex min-w-28 flex-col items-start gap-1 rounded-lg border bg-card px-3 py-2 text-left shadow-xs transition-colors hover:bg-accent",
            value === status && "border-primary ring-1 ring-primary"
          )}
        >
          {value ? (
            <StatusBadge status={value} />
          ) : (
            <span className="text-xs font-medium text-muted-foreground">
              All runs
            </span>
          )}
          <span className="text-xl font-semibold tabular-nums">{n}</span>
        </button>
      ))}
    </div>
  )
}

export function Runs() {
  const search = useSearch({ from: "/app/runs" })
  const navigate = useNavigate()
  const set = (patch: RunsSearch) =>
    void navigate({
      to: "/runs",
      search: { ...search, ...patch },
      replace: true,
    })
  const list = useQuery({
    queryKey: [
      "runs",
      search.workspace,
      search.case,
      search.suite,
      search.status,
      search.submission,
    ],
    queryFn: () =>
      runs.listRuns({
        caseId: search.case ?? "",
        suite: search.suite ?? "",
        status: search.status ?? "",
        submissionId: search.submission ?? "",
        // The server filters before it limits, so an older workspace's runs are not crowded
        // out by a busier one's.
        workspace: search.workspace ?? "",
        limit: 500,
      }),
    refetchInterval: 5_000,
  })
  const all = useMemo(() => list.data?.runs ?? [], [list.data])
  // Every workspace the case library knows, not only those among the runs listed.
  const library = useQuery({
    queryKey: ["cases", true],
    queryFn: () => cases.listCases({ includeArchived: true }),
  })
  const workspaces = useMemo(
    () =>
      [
        ...new Set([
          ...(library.data?.cases ?? []).map((c) => c.workspace),
          ...all.map((r) => r.workspace),
        ]),
      ].sort(),
    [library.data, all]
  )
  const groups = useMemo(() => {
    const out = new Map<string, Run[]>()
    for (const r of all)
      out.set(r.submissionId, [...(out.get(r.submissionId) ?? []), r])
    return [...out]
  }, [all])
  const filtered = Object.values(search).some(Boolean)

  return (
    <Page
      title="Runs"
      description="Every run, grouped by the submission that started it. Refreshed every 5 seconds."
    >
      <Summary
        all={all}
        status={search.status}
        onStatus={(status) => set({ status })}
      />
      <div className="flex flex-wrap items-center gap-2" role="search">
        <NativeSelect
          aria-label="Workspace"
          size="sm"
          className="w-44"
          value={search.workspace ?? ""}
          onChange={(e) => set({ workspace: e.target.value || undefined })}
        >
          <NativeSelectOption value="">All workspaces</NativeSelectOption>
          {workspaces.map((w) => (
            <NativeSelectOption key={w} value={w}>
              {w}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <Input
          aria-label="Case"
          placeholder="Case id"
          className="h-8 w-44"
          key={`case:${search.case}`}
          defaultValue={search.case ?? ""}
          onBlur={(e) => set({ case: e.target.value || undefined })}
        />
        <Input
          aria-label="Suite"
          placeholder="Suite"
          className="h-8 w-44"
          key={`suite:${search.suite}`}
          defaultValue={search.suite ?? ""}
          onBlur={(e) => set({ suite: e.target.value || undefined })}
        />
        <Input
          aria-label="Submission"
          placeholder="Submission"
          className="h-8 w-44 font-mono text-xs"
          key={`submission:${search.submission}`}
          defaultValue={search.submission ?? ""}
          onBlur={(e) => set({ submission: e.target.value || undefined })}
        />
        <NativeSelect
          aria-label="Status"
          size="sm"
          className="w-36"
          value={search.status ?? ""}
          onChange={(e) => set({ status: e.target.value || undefined })}
        >
          <NativeSelectOption value="">Any status</NativeSelectOption>
          {STATUSES.map((s) => (
            <NativeSelectOption key={s} value={s}>
              {s}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        {filtered && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              void navigate({ to: "/runs", search: {}, replace: true })
            }
          >
            <X /> Clear filters
          </Button>
        )}
      </div>
      <ErrorAlert title="Runs did not load" error={list.error} />
      {list.isPending ? (
        <Loading what="runs" />
      ) : groups.length === 0 ? (
        <Empty className="border">
          <EmptyHeader>
            <EmptyTitle>No runs match</EmptyTitle>
            <EmptyDescription>
              Submit a case from Cases, or with `swarm run`.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="flex flex-col gap-4">
          {groups.map(([id, items]) => (
            <Submission key={id} id={id} items={items} />
          ))}
        </div>
      )}
    </Page>
  )
}

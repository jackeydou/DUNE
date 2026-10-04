import { useQuery } from "@tanstack/react-query"
import { Link, useNavigate, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import { analysis, runs, when } from "@/api"
import { ErrorAlert, Loading, Page, StatusBadge } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { Run } from "@/gen/swarmeval/api/v1/run_pb"

const STATUSES = ["queued", "running", "paused", "interrupted", "done", "failed", "cancelled"]

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
  if (report.isPending) return <Loading what="rates" />
  if (report.error) return <ErrorAlert title="The rates did not load" error={report.error} />
  if (report.data.rates.length === 0)
    return <p className="text-sm text-muted-foreground">No run of this submission has ended done and been scored yet.</p>
  return (
    <Table data-testid="rates">
      <TableHeader>
        <TableRow>
          <TableHead>Variant</TableHead>
          <TableHead>Scorer</TableHead>
          <TableHead>Epochs</TableHead>
          <TableHead>Rate</TableHead>
          <TableHead>95% interval</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {report.data.rates.map((r) => (
          <TableRow key={`${r.variant}.${r.scorer}`}>
            <TableCell className="font-mono text-xs">{r.variantValuesJson}</TableCell>
            <TableCell>{r.scorer}</TableCell>
            <TableCell>{r.epochs}</TableCell>
            <TableCell>{pct(r.rate)}</TableCell>
            <TableCell>
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
    <section className="flex flex-col gap-2 rounded-xl border p-4" data-testid="submission">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm">
          <span className="font-medium">
            {first.workspace}/{first.caseId}@{first.caseRevision}
          </span>{" "}
          <span className="text-muted-foreground">
            submission {id}
            {first.suite && ` · suite ${first.suite}`} · {when(first.createdAt)}
            {first.submittedBy && ` · by ${first.submittedBy}`}
          </span>
        </div>
        <Button variant="outline" size="sm" onClick={() => setRates(!rates)}>
          {rates ? "Hide rates" : "Show rates"}
        </Button>
      </div>
      {rates && <SubmissionRates submissionId={id} />}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Run</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Variant</TableHead>
            <TableHead>Epoch</TableHead>
            <TableHead>Finished</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((r) => (
            <TableRow key={r.runId}>
              <TableCell>
                <Link to="/runs/$runId" params={{ runId: r.runId }} className="font-mono text-xs underline-offset-4 hover:underline">
                  {r.runId}
                </Link>
              </TableCell>
              <TableCell>
                <StatusBadge status={r.status} />
              </TableCell>
              <TableCell className="font-mono text-xs">{JSON.stringify(r.variantValues ?? {})}</TableCell>
              <TableCell>
                {r.epoch} of {r.epochs}
              </TableCell>
              <TableCell>{when(r.finishedAt)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  )
}

export function Runs() {
  const search = useSearch({ from: "/app/runs" })
  const navigate = useNavigate()
  const set = (patch: RunsSearch) =>
    void navigate({ to: "/runs", search: { ...search, ...patch }, replace: true })
  const list = useQuery({
    queryKey: ["runs", search.case, search.suite, search.status, search.submission],
    queryFn: () =>
      runs.listRuns({
        caseId: search.case ?? "",
        suite: search.suite ?? "",
        status: search.status ?? "",
        submissionId: search.submission ?? "",
        limit: 500,
      }),
    refetchInterval: 5_000,
  })
  const all = useMemo(() => list.data?.runs ?? [], [list.data])
  const workspaces = useMemo(() => [...new Set(all.map((r) => r.workspace))].sort(), [all])
  const groups = useMemo(() => {
    const out = new Map<string, Run[]>()
    for (const r of all) {
      if (search.workspace && r.workspace !== search.workspace) continue
      out.set(r.submissionId, [...(out.get(r.submissionId) ?? []), r])
    }
    return [...out]
  }, [all, search.workspace])

  return (
    <Page title="Runs">
      <div className="flex flex-wrap items-end gap-3">
        <Field className="w-44">
          <FieldLabel htmlFor="filter-workspace">Workspace</FieldLabel>
          <NativeSelect id="filter-workspace" value={search.workspace ?? ""} onChange={(e) => set({ workspace: e.target.value || undefined })}>
            <NativeSelectOption value="">All</NativeSelectOption>
            {workspaces.map((w) => (
              <NativeSelectOption key={w} value={w}>
                {w}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field className="w-44">
          <FieldLabel htmlFor="filter-case">Case</FieldLabel>
          <Input id="filter-case" defaultValue={search.case ?? ""} onBlur={(e) => set({ case: e.target.value || undefined })} />
        </Field>
        <Field className="w-52">
          <FieldLabel htmlFor="filter-suite">Suite</FieldLabel>
          <Input id="filter-suite" defaultValue={search.suite ?? ""} onBlur={(e) => set({ suite: e.target.value || undefined })} />
        </Field>
        <Field className="w-40">
          <FieldLabel htmlFor="filter-submission">Submission</FieldLabel>
          <Input id="filter-submission" key={search.submission} defaultValue={search.submission ?? ""} onBlur={(e) => set({ submission: e.target.value || undefined })} />
        </Field>
        <Field className="w-40">
          <FieldLabel htmlFor="filter-status">Status</FieldLabel>
          <NativeSelect id="filter-status" value={search.status ?? ""} onChange={(e) => set({ status: e.target.value || undefined })}>
            <NativeSelectOption value="">All</NativeSelectOption>
            {STATUSES.map((s) => (
              <NativeSelectOption key={s} value={s}>
                {s}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
      </div>
      <ErrorAlert title="Runs did not load" error={list.error} />
      {list.isPending ? (
        <Loading what="runs" />
      ) : groups.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No runs match</EmptyTitle>
            <EmptyDescription>Submit a case from Cases, or with `swarm run`.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        groups.map(([id, items]) => <Submission key={id} id={id} items={items} />)
      )}
    </Page>
  )
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import { runs, when } from "@/api"
import { ErrorAlert, Loading, Page, StatusBadge } from "@/components/common"
import { Replay, StreamedReplay } from "@/components/Replay"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { scoresOf, type RunEvent } from "@/lib/events"
import { useRunEvents } from "@/lib/useRunEvents"

const FORKABLE = ["done", "cancelled"]
const FINISHED = ["done", "failed", "cancelled", "interrupted"]

function Scores({ events }: { events: readonly RunEvent[] }) {
  const scores = useMemo(() => scoresOf(events), [events])
  if (scores.length === 0) return <p className="text-sm text-muted-foreground">No scores yet. Scorers run after the agents stop.</p>
  return (
    <Table data-testid="scores">
      <TableHeader>
        <TableRow>
          <TableHead>Scorer</TableHead>
          <TableHead>Value</TableHead>
          <TableHead>Explanation</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {scores.map((s) => (
          <TableRow key={s.scorer}>
            <TableCell>{s.scorer}</TableCell>
            <TableCell className="font-mono">{s.value}</TableCell>
            <TableCell className="whitespace-pre-wrap">{s.explanation}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function RunDetail() {
  const { runId } = useParams({ from: "/app/runs/$runId" })
  const client = useQueryClient()
  const navigate = useNavigate()
  const [other, setOther] = useState("")
  // One stream for the scores and the replay.
  const stream = useRunEvents(runId)
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: async () => (await runs.getRun({ runId })).run,
    refetchInterval: (q) => (q.state.data && FINISHED.includes(q.state.data.status) ? false : 2_000),
  })
  const refresh = () => client.invalidateQueries({ queryKey: ["run", runId] })
  const cancel = useMutation({ mutationFn: () => runs.cancelRun({ runId }), onSuccess: refresh })
  const resume = useMutation({ mutationFn: () => runs.resumeRun({ runId }), onSuccess: refresh })

  if (run.error) return <Page title={runId}><ErrorAlert title="The run did not load" error={run.error} /></Page>
  if (!run.data) return <Page title={runId}><Loading what="the run" /></Page>
  const r = run.data
  const facts: [string, React.ReactNode][] = [
    ["Case", <Link to="/cases/$workspace/$caseId" params={{ workspace: r.workspace, caseId: r.caseId }} className="underline">{r.workspace}/{r.caseId}@{r.caseRevision}</Link>],
    ["Submission", <Link to="/runs" search={{ submission: r.submissionId }} className="underline">{r.submissionId}</Link>],
    ["Variant", <span className="font-mono text-xs">{r.variant} {JSON.stringify(r.variantValues ?? {})}</span>],
    ["Epoch", `${r.epoch} of ${r.epochs}`],
    ["Isolation", r.isolation || "—"],
    ["Created", when(r.createdAt)],
    ["Started", when(r.startedAt)],
    ["Finished", when(r.finishedAt)],
    ["Submitted by", r.submittedBy || "—"],
  ]
  if (r.suite) facts.push(["Suite", r.suite])
  if (r.replaces) facts.push(["Reruns", <Link to="/runs/$runId" params={{ runId: r.replaces }} className="underline">{r.replaces}</Link>])
  if (r.forkedFrom)
    facts.push(["Forked from", <span><Link to="/runs/$runId" params={{ runId: r.forkedFrom }} className="underline">{r.forkedFrom}</Link> after seq {String(r.forkSeq)} ({r.fidelity || "not restored yet"})</span>])
  if (r.cancelledBy) facts.push(["Cancelled by", r.cancelledBy])
  if (r.resumedBy) facts.push(["Resumed by", r.resumedBy])

  return (
    <Page
      title={<span className="flex items-center gap-3 font-mono text-base">{r.runId} <StatusBadge status={r.status} /></span>}
      actions={
        <>
          {r.status === "paused" && <Button onClick={() => resume.mutate()} disabled={resume.isPending}>Resume</Button>}
          {["queued", "running", "paused"].includes(r.status) && (
            <Button variant="destructive" onClick={() => cancel.mutate()} disabled={cancel.isPending}>Cancel</Button>
          )}
        </>
      }
    >
      <ErrorAlert title="The run was not changed" error={cancel.error ?? resume.error} />
      {r.error && <ErrorAlert title={`The run ${r.status === "interrupted" ? "was interrupted" : "failed"}`} error={r.error} />}
      <dl className="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-1 text-sm">
        {facts.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-muted-foreground">{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <h2 className="font-heading font-medium">Scores</h2>
      <Scores events={stream.events} />
      <div className="flex items-end justify-between gap-4">
        <h2 className="font-heading font-medium">Replay</h2>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (other) void navigate({ to: "/compare", search: { a: runId, b: other } })
          }}
        >
          <Input aria-label="Run to compare with" placeholder="another run id" className="w-72 font-mono text-xs" value={other} onChange={(e) => setOther(e.target.value)} />
          <Button type="submit" variant="outline" size="sm">Compare side by side</Button>
        </form>
      </div>
      <Replay runId={runId} stream={stream} canFork={FORKABLE.includes(r.status)} />
    </Page>
  )
}

export function Compare() {
  const { a, b } = useSearch({ from: "/app/compare" })
  return (
    <Page title="Compare runs">
      <div className="grid grid-cols-2 gap-4">
        {[a, b].map((runId) => (
          <div key={runId} className="flex min-w-0 flex-col gap-2">
            <Link to="/runs/$runId" params={{ runId }} className="font-mono text-xs underline">{runId}</Link>
            <StreamedReplay runId={runId} />
          </div>
        ))}
      </div>
    </Page>
  )
}

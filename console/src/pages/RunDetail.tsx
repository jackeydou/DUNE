import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router"
import { Columns2, Play, Square } from "lucide-react"
import { useMemo, useState } from "react"

import { runs, when } from "@/api"
import {
  ErrorAlert,
  Facts,
  Id,
  Loading,
  Page,
  Section,
  StatusBadge,
  VariantChips,
} from "@/components/common"
import { Replay, StreamedReplay } from "@/components/Replay"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { scoresOf, type RunEvent } from "@/lib/events"
import { ago, duration } from "@/lib/time"
import { modelsOf } from "@/lib/models"
import { useRunEvents } from "@/lib/useRunEvents"

const FORKABLE = ["done", "cancelled"]
const FINISHED = ["done", "failed", "cancelled", "interrupted"]

const runsCrumb = <Link to="/runs">Runs</Link>

function Scores({ events }: { events: readonly RunEvent[] }) {
  const scores = useMemo(() => scoresOf(events), [events])
  if (scores.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        No scores yet. Scorers run after the agents stop.
      </p>
    )
  return (
    <div className="flex flex-col gap-2" data-testid="scores">
      {scores.map((s) => (
        <div
          key={s.scorer}
          className="flex flex-col gap-1 rounded-lg border bg-background p-3"
        >
          <div className="flex items-baseline justify-between gap-3">
            <span className="truncate text-sm font-medium">{s.scorer}</span>
            <span className="font-mono text-lg font-semibold">
              {s.value || "—"}
            </span>
          </div>
          {s.explanation && (
            <p className="text-xs whitespace-pre-wrap text-muted-foreground">
              {s.explanation}
            </p>
          )}
        </div>
      ))}
    </div>
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
    refetchInterval: (q) =>
      q.state.data && FINISHED.includes(q.state.data.status) ? false : 2_000,
  })
  const refresh = () => client.invalidateQueries({ queryKey: ["run", runId] })
  const cancel = useMutation({
    mutationFn: () => runs.cancelRun({ runId }),
    onSuccess: refresh,
  })
  const resume = useMutation({
    mutationFn: () => runs.resumeRun({ runId }),
    onSuccess: refresh,
  })

  if (run.error)
    return (
      <Page title={runId} crumbs={[runsCrumb]}>
        <ErrorAlert title="The run did not load" error={run.error} />
      </Page>
    )
  if (!run.data)
    return (
      <Page title={runId} crumbs={[runsCrumb]}>
        <Loading what="the run" />
      </Page>
    )
  const r = run.data
  const at = (ts: typeof r.createdAt) => <span title={when(ts)}>{ago(ts)}</span>
  const facts: [string, React.ReactNode][] = [
    [
      "Case",
      <Link
        to="/cases/$workspace/$caseId"
        params={{ workspace: r.workspace, caseId: r.caseId }}
        className="font-mono text-primary underline-offset-4 hover:underline"
      >
        {r.workspace}/{r.caseId}@{r.caseRevision}
      </Link>,
    ],
    [
      "Variant",
      <span className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-muted-foreground">#{r.variant}</span>
        <VariantChips values={r.variantValues} />
      </span>,
    ],
    ["Models", <VariantChips values={modelsOf(r.variantValues)} />],
    ["Epoch", `${r.epoch} of ${r.epochs}`],
    ["Duration", duration(r.startedAt, r.finishedAt)],
    ["Created", at(r.createdAt)],
    ["Started", at(r.startedAt)],
    ["Finished", at(r.finishedAt)],
    ["Isolation", r.isolation || "—"],
    ["Submitted by", r.submittedBy || "—"],
  ]
  if (r.suite) facts.push(["Suite", r.suite])
  if (r.replaces)
    facts.push([
      "Reruns",
      <Link
        to="/runs/$runId"
        params={{ runId: r.replaces }}
        className="font-mono text-primary hover:underline"
      >
        {r.replaces}
      </Link>,
    ])
  if (r.forkedFrom)
    facts.push([
      "Forked from",
      <span>
        <Link
          to="/runs/$runId"
          params={{ runId: r.forkedFrom }}
          className="font-mono text-primary hover:underline"
        >
          {r.forkedFrom}
        </Link>{" "}
        after seq {String(r.forkSeq)} ({r.fidelity || "not restored yet"})
      </span>,
    ])
  if (r.cancelledBy) facts.push(["Cancelled by", r.cancelledBy])
  if (r.resumedBy) facts.push(["Resumed by", r.resumedBy])

  return (
    <Page
      title={
        <span className="flex items-center gap-3 text-lg">
          <Id value={r.runId} /> <StatusBadge status={r.status} />
        </span>
      }
      label={r.runId}
      crumbs={[
        runsCrumb,
        <Link
          to="/runs"
          search={{ submission: r.submissionId }}
          className="font-mono"
        >
          {r.submissionId}
        </Link>,
      ]}
      actions={
        <>
          {r.status === "paused" && (
            <Button onClick={() => resume.mutate()} disabled={resume.isPending}>
              <Play /> Resume
            </Button>
          )}
          {["queued", "running", "paused"].includes(r.status) && (
            <Button
              variant="destructive"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
            >
              <Square /> Cancel
            </Button>
          )}
        </>
      }
    >
      <ErrorAlert
        title="The run was not changed"
        error={cancel.error ?? resume.error}
      />
      {r.error && (
        <ErrorAlert
          title={`The run ${r.status === "interrupted" ? "was interrupted" : "failed"}`}
          error={r.error}
        />
      )}
      <div className="grid gap-4 lg:grid-cols-3">
        <Section title="Overview" className="lg:col-span-2">
          <Facts items={facts} />
        </Section>
        <Section title="Scores" description="Each scorer's last score.">
          <Scores events={stream.events} />
        </Section>
      </div>
      <Section
        title="Replay"
        description="Every event in order, one lane per agent. Pick one to see its payload and causes."
        actions={
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (other)
                void navigate({
                  to: "/compare",
                  search: { a: runId, b: other },
                })
            }}
          >
            <Input
              aria-label="Run to compare with"
              placeholder="another run id"
              className="h-8 w-64 font-mono text-xs"
              value={other}
              onChange={(e) => setOther(e.target.value)}
            />
            <Button type="submit" variant="outline" size="sm">
              <Columns2 /> Compare side by side
            </Button>
          </form>
        }
      >
        <Replay
          runId={runId}
          stream={stream}
          canFork={FORKABLE.includes(r.status)}
        />
      </Section>
    </Page>
  )
}

export function Compare() {
  const { a, b } = useSearch({ from: "/app/compare" })
  return (
    <Page
      title="Compare runs"
      crumbs={[runsCrumb]}
      description="Two runs' replays side by side."
    >
      <div className="grid grid-cols-2 gap-4">
        {[a, b].map((runId) => (
          <Section
            key={runId}
            title={
              <Link
                to="/runs/$runId"
                params={{ runId }}
                className="font-mono text-primary hover:underline"
              >
                {runId}
              </Link>
            }
            className="min-w-0"
          >
            <StreamedReplay runId={runId} />
          </Section>
        ))}
      </div>
    </Page>
  )
}

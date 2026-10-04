import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import { analysis, runs } from "@/api"
import { ErrorAlert, Loading, Verbatim } from "@/components/common"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { fromJson } from "@bufbuild/protobuf"
import { ForkEditSchema } from "@/gen/swarmeval/api/v1/run_pb"
import { lanesOf, prettyPayload, RUN_LANE, typesOf, type RunEvent } from "@/lib/events"
import { useRunEvents, type RunEvents } from "@/lib/useRunEvents"

/** The chain of causes of one event, from the analysis service: exported runs only. */
function Trace({ runId, eventId }: { runId: string; eventId: string }) {
  const trace = useQuery({
    queryKey: ["trace", runId, eventId],
    queryFn: () => analysis.getTrace({ runId, eventId }),
    retry: false,
  })
  if (trace.isPending) return <Loading what="the causal chain" />
  if (trace.error)
    return <ErrorAlert title="No causal chain yet: it is read from the run's export, written when the run ends" error={trace.error} />
  return (
    <ol className="flex flex-col gap-1 text-xs" data-testid="trace">
      {trace.data.links.map((link) => (
        <li key={`${link.runId}:${link.eventId}`} className="rounded-md border p-2 font-mono">
          <span className="text-muted-foreground">
            {link.runId !== runId && (
              <Link to="/runs/$runId" params={{ runId: link.runId }} className="underline">
                {link.runId}
              </Link>
            )}{" "}
            #{String(link.seq)} {link.agentId || RUN_LANE}
          </span>{" "}
          {link.line}
        </li>
      ))}
    </ol>
  )
}

function ForkDialog({ runId, event, onClose }: { runId: string; event: RunEvent; onClose: () => void }) {
  const navigate = useNavigate()
  const [edits, setEdits] = useState("[]")
  const fork = useMutation({
    mutationFn: async () => {
      const parsed: unknown = JSON.parse(edits)
      if (!Array.isArray(parsed)) throw new Error("The edits must be a JSON list.")
      return runs.forkRun({
        runId,
        atEventId: event.eventId,
        edits: parsed.map((edit) => fromJson(ForkEditSchema, edit)),
      })
    },
    onSuccess: (res) => {
      if (res.run) void navigate({ to: "/runs/$runId", params: { runId: res.run.runId } })
    },
  })
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Fork from event #{String(event.seq)}</DialogTitle>
          <DialogDescription>
            A new run goes on from this run's state at the start of the turn this event happened in, with the edits applied.
          </DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel htmlFor="fork-edits">Edits</FieldLabel>
          <Textarea id="fork-edits" className="font-mono text-xs" rows={8} value={edits} onChange={(e) => setEdits(e.target.value)} />
          <FieldDescription>
            A JSON list. Each item is one of {`{"replaceMessage": {"agentId", "index", "content"}}`},{" "}
            {`{"deleteMessage": {"agentId", "index"}}`}, {`{"replaceDelivery": {"sendEventId", "recipient", "content"}}`}. An
            empty list reruns from here unchanged.
          </FieldDescription>
        </Field>
        <ErrorAlert title="The run was not forked" error={fork.error} />
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => fork.mutate()} disabled={fork.isPending}>
            Fork
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function EventDetail({ runId, event, canFork, onClose }: { runId: string; event: RunEvent; canFork: boolean; onClose: () => void }) {
  const [forking, setForking] = useState(false)
  return (
    <aside className="flex w-[28rem] shrink-0 flex-col gap-3 rounded-xl border p-4" data-testid="event-detail">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium">
          #{String(event.seq)} <Badge variant="outline">{event.type}</Badge> {event.agentId || RUN_LANE}
        </div>
        <div className="flex gap-2">
          {canFork && (
            <Button size="sm" variant="outline" onClick={() => setForking(true)}>
              Fork from here
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
      <div className="font-mono text-xs text-muted-foreground">{event.eventId}</div>
      <h3 className="text-sm font-medium">Causal chain</h3>
      <Trace runId={runId} eventId={event.eventId} />
      <h3 className="text-sm font-medium">Stored event</h3>
      <Verbatim className="max-h-[32rem]">{prettyPayload(event)}</Verbatim>
      {forking && <ForkDialog runId={runId} event={event} onClose={() => setForking(false)} />}
    </aside>
  )
}

/** One run's events in order, a lane per agent, appended live while the run goes on. */
export function Replay({ runId, stream, canFork, detail = true }: { runId: string; stream: RunEvents; canFork: boolean; detail?: boolean }) {
  const { events, live, error } = stream
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string>()
  const lanes = useMemo(() => lanesOf(events), [events])
  const types = useMemo(() => typesOf(events), [events])
  const shown = useMemo(() => events.filter((e) => !hidden.has(e.type)), [events, hidden])
  const current = events.find((e) => e.eventId === selected)

  return (
    <div className="flex flex-col gap-3" data-testid="replay">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="text-muted-foreground" data-testid="event-count">
          {events.length} events
        </span>
        {live && (
          <span className="flex items-center gap-1 text-muted-foreground">
            <Spinner /> live
          </span>
        )}
        {types.map((type) => (
          <label key={type} className="flex items-center gap-1.5">
            <Checkbox
              checked={!hidden.has(type)}
              onCheckedChange={(on) => {
                const next = new Set(hidden)
                if (on === true) next.delete(type)
                else next.add(type)
                setHidden(next)
              }}
            />
            {type}
          </label>
        ))}
      </div>
      <ErrorAlert title="The event stream stopped" error={error} />
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1 overflow-auto rounded-xl border">
          <div className="grid min-w-max text-xs" style={{ gridTemplateColumns: `4rem repeat(${lanes.length}, minmax(14rem, 1fr))` }}>
            <div className="sticky top-0 border-b bg-muted p-2 font-medium">seq</div>
            {lanes.map((lane) => (
              <div key={lane} className="sticky top-0 border-b border-l bg-muted p-2 font-medium" data-testid="lane">
                {lane === RUN_LANE ? "run" : lane}
              </div>
            ))}
            {shown.map((e) => {
              const column = lanes.indexOf(e.agentId || RUN_LANE)
              return (
                <div key={e.eventId} className="contents">
                  <div className="border-b p-2 font-mono text-muted-foreground">#{String(e.seq)}</div>
                  {lanes.map((lane, i) => (
                    <div key={lane} className="border-b border-l p-1">
                      {i === column && (
                        <button
                          type="button"
                          data-testid="event"
                          data-type={e.type}
                          onClick={() => detail && setSelected(e.eventId)}
                          className={`w-full rounded-md p-1 text-left font-mono break-words whitespace-pre-wrap hover:bg-muted ${selected === e.eventId ? "bg-muted ring-1 ring-ring" : ""}`}
                        >
                          {e.line}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
        {detail && current && <EventDetail runId={runId} event={current} canFork={canFork} onClose={() => setSelected(undefined)} />}
      </div>
    </div>
  )
}

/** `Replay` with a stream of its own, for a page that shows nothing else of the run. */
export function StreamedReplay({ runId }: { runId: string }) {
  return <Replay runId={runId} stream={useRunEvents(runId)} canFork={false} detail={false} />
}

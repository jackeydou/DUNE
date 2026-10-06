import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { GitFork, MousePointerClick, X } from "lucide-react"
import { useMemo, useState } from "react"

import { analysis, runs } from "@/api"
import { ErrorAlert, Id, Loading, Verbatim } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { fromJson } from "@bufbuild/protobuf"
import { ForkEditSchema } from "@/gen/swarmeval/api/v1/run_pb"
import {
  lanesOf,
  prettyPayload,
  RUN_LANE,
  typesOf,
  type RunEvent,
} from "@/lib/events"
import { useRunEvents, type RunEvents } from "@/lib/useRunEvents"
import { cn } from "@/lib/utils"

/** A colour per family of event types, as the dot before the type's name. */
function tone(type: string): string {
  if (type === "model") return "bg-chart-2"
  if (type === "tool") return "bg-info"
  if (type === "score") return "bg-success"
  if (type === "error" || type.endsWith("alert")) return "bg-danger"
  if (type.startsWith("swarmeval.")) return "bg-primary"
  return "bg-muted-foreground/50"
}

function TypeTag({ type }: { type: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-sans text-[11px] font-medium text-muted-foreground">
      <i className={cn("size-2 rounded-full", tone(type))} aria-hidden />
      {type}
    </span>
  )
}

/** The chain of causes of one event, from the analysis service: exported runs only. */
function Trace({ runId, eventId }: { runId: string; eventId: string }) {
  const trace = useQuery({
    queryKey: ["trace", runId, eventId],
    queryFn: () => analysis.getTrace({ runId, eventId }),
    retry: false,
  })
  if (trace.isPending) return <Loading what="the causal chain" />
  if (trace.error)
    return (
      <ErrorAlert
        title="No causal chain yet: it is read from the run's export, written when the run ends"
        error={trace.error}
      />
    )
  return (
    <ol
      className="relative flex flex-col gap-2 border-l pl-4 text-xs"
      data-testid="trace"
    >
      {trace.data.links.map((link) => (
        <li
          key={`${link.runId}:${link.eventId}`}
          className="relative rounded-md border bg-background p-2 font-mono"
        >
          <i
            className="absolute top-3 -left-[1.3rem] size-2 rounded-full border-2 border-background bg-primary"
            aria-hidden
          />
          <div className="mb-1 text-muted-foreground">
            {link.runId !== runId && (
              <Link
                to="/runs/$runId"
                params={{ runId: link.runId }}
                className="underline"
              >
                {link.runId}
              </Link>
            )}{" "}
            #{String(link.seq)} · {link.agentId || RUN_LANE}
          </div>
          <div className="break-words whitespace-pre-wrap">{link.line}</div>
        </li>
      ))}
    </ol>
  )
}

function ForkDialog({
  runId,
  event,
  onClose,
}: {
  runId: string
  event: RunEvent
  onClose: () => void
}) {
  const navigate = useNavigate()
  const [edits, setEdits] = useState("[]")
  const fork = useMutation({
    mutationFn: async () => {
      const parsed: unknown = JSON.parse(edits)
      if (!Array.isArray(parsed))
        throw new Error("The edits must be a JSON list.")
      return runs.forkRun({
        runId,
        atEventId: event.eventId,
        edits: parsed.map((edit) => fromJson(ForkEditSchema, edit)),
      })
    },
    onSuccess: (res) => {
      if (res.run)
        void navigate({ to: "/runs/$runId", params: { runId: res.run.runId } })
    },
  })
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Fork from event #{String(event.seq)}</DialogTitle>
          <DialogDescription>
            A new run goes on from this run's state at the start of the turn
            this event happened in, with the edits applied.
          </DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel htmlFor="fork-edits">Edits</FieldLabel>
          <Textarea
            id="fork-edits"
            className="font-mono text-xs"
            rows={8}
            value={edits}
            onChange={(e) => setEdits(e.target.value)}
          />
          <FieldDescription>
            A JSON list. Each item is one of{" "}
            {`{"replaceMessage": {"agentId", "index", "content"}}`},{" "}
            {`{"deleteMessage": {"agentId", "index"}}`},{" "}
            {`{"replaceDelivery": {"sendEventId", "recipient", "content"}}`}. An
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

function EventDetail({
  runId,
  event,
  canFork,
  onClose,
}: {
  runId: string
  event: RunEvent
  canFork: boolean
  onClose: () => void
}) {
  const [forking, setForking] = useState(false)
  return (
    <aside
      className="flex w-[28rem] shrink-0 flex-col overflow-hidden border-l bg-background"
      data-testid="event-detail"
    >
      <div className="flex items-center justify-between gap-2 border-b px-4 py-2">
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="font-mono font-medium">#{String(event.seq)}</span>
          <TypeTag type={event.type} />
          <span className="truncate text-muted-foreground">
            {event.agentId || "run"}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {canFork && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setForking(true)}
            >
              <GitFork /> Fork from here
            </Button>
          )}
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={onClose}
            aria-label="Close"
          >
            <X />
          </Button>
        </div>
      </div>
      <div className="flex flex-col gap-4 overflow-auto p-4">
        <Id value={event.eventId} className="text-xs text-muted-foreground" />
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Causal chain
          </h3>
          <Trace runId={runId} eventId={event.eventId} />
        </div>
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Stored event
          </h3>
          <Verbatim>{prettyPayload(event)}</Verbatim>
        </div>
      </div>
      {forking && (
        <ForkDialog
          runId={runId}
          event={event}
          onClose={() => setForking(false)}
        />
      )}
    </aside>
  )
}

/** One run's events in order, a lane per agent, appended live while the run goes on. The lanes
 * and the chosen event's detail scroll apart, in a panel of fixed height. */
export function Replay({
  runId,
  stream,
  canFork,
  detail = true,
}: {
  runId: string
  stream: RunEvents
  canFork: boolean
  detail?: boolean
}) {
  const { events, live, error } = stream
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string>()
  const lanes = useMemo(() => lanesOf(events), [events])
  const types = useMemo(() => typesOf(events), [events])
  const shown = useMemo(
    () => events.filter((e) => !hidden.has(e.type)),
    [events, hidden]
  )
  const current = events.find((e) => e.eventId === selected)

  return (
    <div className="flex flex-col gap-3" data-testid="replay">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className="mr-1 font-medium tabular-nums"
          data-testid="event-count"
        >
          {events.length} events
        </span>
        {live && (
          <span className="mr-1 inline-flex items-center gap-1 rounded-full bg-info/10 px-2 py-0.5 text-xs font-medium text-info">
            <Spinner className="size-3" /> live
          </span>
        )}
        {types.map((type) => (
          <label
            key={type}
            className={cn(
              "flex cursor-pointer items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors hover:bg-accent",
              hidden.has(type) && "text-muted-foreground opacity-60"
            )}
          >
            <Checkbox
              className="size-3.5"
              checked={!hidden.has(type)}
              onCheckedChange={(on) => {
                const next = new Set(hidden)
                if (on === true) next.delete(type)
                else next.add(type)
                setHidden(next)
              }}
            />
            <i className={cn("size-2 rounded-full", tone(type))} aria-hidden />
            {type}
          </label>
        ))}
      </div>
      <ErrorAlert title="The event stream stopped" error={error} />
      <div className="flex h-[calc(100svh-12rem)] min-h-[28rem] overflow-hidden rounded-xl border bg-card shadow-xs">
        <div className="min-w-0 flex-1 overflow-auto">
          <div
            className="grid text-xs"
            style={{
              gridTemplateColumns: `4rem repeat(${lanes.length}, minmax(14rem, 1fr))`,
              minWidth: `${4 + lanes.length * 14}rem`,
            }}
          >
            <div className="sticky top-0 z-10 border-b bg-muted px-3 py-2 font-medium text-muted-foreground">
              seq
            </div>
            {lanes.map((lane) => (
              <div
                key={lane}
                className="sticky top-0 z-10 border-b border-l bg-muted px-3 py-2 font-medium"
                data-testid="lane"
              >
                {lane === RUN_LANE ? "run" : lane}
              </div>
            ))}
            {shown.map((e) => {
              const column = lanes.indexOf(e.agentId || RUN_LANE)
              return (
                <div key={e.eventId} className="contents">
                  <div className="border-b px-3 py-2 font-mono text-muted-foreground tabular-nums">
                    #{String(e.seq)}
                  </div>
                  {lanes.map((lane, i) => (
                    <div key={lane} className="min-w-0 border-b border-l p-1">
                      {i === column && (
                        <button
                          type="button"
                          data-testid="event"
                          data-type={e.type}
                          onClick={() => detail && setSelected(e.eventId)}
                          className={cn(
                            "flex w-full flex-col gap-1 rounded-md px-2 py-1.5 text-left hover:bg-accent",
                            selected === e.eventId &&
                              "bg-primary/10 ring-1 ring-primary"
                          )}
                        >
                          <TypeTag type={e.type} />
                          <span className="line-clamp-4 font-mono break-words whitespace-pre-wrap">
                            {e.line}
                          </span>
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
        {detail &&
          (current ? (
            <EventDetail
              runId={runId}
              event={current}
              canFork={canFork}
              onClose={() => setSelected(undefined)}
            />
          ) : (
            <aside className="hidden w-72 shrink-0 flex-col items-center justify-center gap-2 border-l p-6 text-center text-sm text-muted-foreground xl:flex">
              <MousePointerClick className="size-5" />
              Pick an event to see its stored payload and what caused it.
            </aside>
          ))}
      </div>
    </div>
  )
}

/** `Replay` with a stream of its own, for a page that shows nothing else of the run. */
export function StreamedReplay({ runId }: { runId: string }) {
  return (
    <Replay
      runId={runId}
      stream={useRunEvents(runId)}
      canFork={false}
      detail={false}
    />
  )
}

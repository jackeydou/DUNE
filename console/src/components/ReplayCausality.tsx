import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect, useId, useMemo, useRef } from "react"

import { analysis } from "@/api"
import {
  localTrace,
  type ReplayEvent,
  type TraceBoundary,
  type TraceStep,
} from "@/lib/replay"
import { cn } from "@/lib/utils"

interface CausalityProps {
  runId: string
  event: ReplayEvent
  byId: ReadonlyMap<string, ReplayEvent>
  firstId: string | undefined
  exported: boolean
  onSelect: (id: string) => void
}

function BoundaryNote({ boundary }: { boundary: TraceBoundary }) {
  return (
    <p className="text-xs text-muted-foreground">
      {boundary.kind === "source" ? (
        <>
          Earlier events are in the{" "}
          <Link
            to="/runs/$runId"
            params={{ runId: boundary.runId }}
            className="underline"
          >
            source run
          </Link>
          .
        </>
      ) : boundary.kind === "cycle" ? (
        "This chain stops at a repeated event."
      ) : boundary.kind === "unrecorded" ? (
        "The log does not record an earlier parent."
      ) : (
        "The parent event is absent from the loaded log."
      )}
    </p>
  )
}

function StepLabel({ step, source }: { step: TraceStep; source: boolean }) {
  return (
    <span
      className="font-mono text-[11px] text-muted-foreground"
      title={source ? step.runId : undefined}
    >
      #{String(step.seq)} · {step.agentId || "Run"}
      {source && " · Source"}
    </span>
  )
}

function StepContent({
  step,
  runId,
  current,
  byId,
  onSelect,
  map,
}: {
  step: TraceStep
  runId: string
  current: boolean
  byId: ReadonlyMap<string, ReplayEvent>
  onSelect: (id: string) => void
  map: boolean
}) {
  const local = step.runId === runId ? byId.get(step.eventId) : undefined
  const title = local?.title ?? (step.line || "Event")
  const className = cn(
    "flex w-full min-w-0 flex-col gap-0.5 rounded-md px-2 py-1.5 text-left text-xs break-words",
    map && "h-full justify-center bg-muted/55",
    current && "bg-accent font-medium",
    "hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
  )
  const text = (
    <>
      <StepLabel step={step} source={step.runId !== runId} />
      <span className={map ? "line-clamp-2" : undefined}>
        {map ? title : step.line || title}
      </span>
    </>
  )
  if (local)
    return (
      <button
        type="button"
        className={className}
        aria-current={current ? "step" : undefined}
        onClick={() => onSelect(step.eventId)}
      >
        {text}
      </button>
    )
  if (step.runId !== runId)
    return (
      <Link
        className={className}
        to="/runs/$runId"
        params={{ runId: step.runId }}
      >
        {text}
        <span className="sr-only">Open source run</span>
      </Link>
    )
  return <div className={className}>{text}</div>
}

function CausalMap({
  steps,
  props,
}: {
  steps: TraceStep[]
  props: CausalityProps
}) {
  const arrow = useId()
  const scroll = useRef<HTMLDivElement>(null)
  const agents = [...new Set(steps.map((step) => step.agentId))]
  const columnWidth = 154
  const rowHeight = 70
  const nodeHeight = 53
  const width = agents.length * columnWidth
  const height = steps.length * rowHeight
  const columns = new Map(agents.map((agent, index) => [agent, index]))
  const x = (step: TraceStep) =>
    ((columns.get(step.agentId) ?? 0) + 0.5) * columnWidth
  const selectedColumn = x(steps[steps.length - 1]) / width

  useEffect(() => {
    const pane = scroll.current
    if (!pane) return
    pane.scrollTop = height
    pane.scrollLeft = selectedColumn * pane.scrollWidth - pane.clientWidth / 2
  }, [height, selectedColumn, props.event.event.eventId])

  return (
    <div ref={scroll} className="min-h-0 flex-1 overflow-auto">
      <div
        className="relative mx-auto w-full"
        style={{ minWidth: width, maxWidth: Math.max(420, width), height }}
      >
        <svg
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 size-full text-muted-foreground/70"
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          fill="none"
        >
          <defs>
            <marker
              id={arrow}
              viewBox="0 0 6 6"
              refX="5"
              refY="3"
              markerWidth="6"
              markerHeight="6"
              orient="auto"
            >
              <path d="M0 0 L6 3 L0 6Z" fill="currentColor" />
            </marker>
          </defs>
          {steps.slice(1).map((step, i) => {
            const from = x(steps[i])
            const to = x(step)
            const y = i * rowHeight + nodeHeight
            return (
              <path
                key={`${step.runId}:${step.eventId}`}
                d={`M ${from} ${y} C ${from} ${y + 10}, ${to} ${y + 5}, ${to} ${(i + 1) * rowHeight - 3}`}
                stroke="currentColor"
                strokeWidth="1"
                vectorEffect="non-scaling-stroke"
                markerEnd={`url(#${arrow})`}
              />
            )
          })}
        </svg>
        {steps.map((step, i) => (
          <div
            key={`${step.runId}:${step.eventId}`}
            className="absolute px-2"
            style={{
              top: i * rowHeight,
              height: nodeHeight,
              width: `${100 / agents.length}%`,
              left: `${((x(step) - columnWidth / 2) / width) * 100}%`,
            }}
          >
            <StepContent
              step={step}
              runId={props.runId}
              byId={props.byId}
              current={
                step.runId === props.runId &&
                step.eventId === props.event.event.eventId
              }
              onSelect={props.onSelect}
              map
            />
          </div>
        ))}
      </div>
    </div>
  )
}

export function ReplayCausality({
  map = false,
  ...props
}: CausalityProps & { map?: boolean }) {
  const { runId, event, byId, firstId, exported, onSelect } = props
  const eventId = event.event.eventId
  const trace = useQuery({
    queryKey: ["trace", runId, eventId],
    queryFn: () => analysis.getTrace({ runId, eventId }),
    enabled: exported,
    retry: false,
  })
  const local = useMemo(
    () => localTrace(runId, byId, event, firstId),
    [runId, byId, event, firstId]
  )
  const fromExport = trace.data?.links.length ? trace.data.links : undefined
  const steps = fromExport ?? local.steps
  const boundary: TraceBoundary | undefined = fromExport
    ? fromExport[0].seq > 1n
      ? { kind: "unrecorded" }
      : undefined
    : local.boundary

  return (
    <div className={cn("flex flex-col gap-3", map && "min-h-0 flex-1")}>
      {exported && trace.isError && (
        <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
          Showing recorded events. The exported chain could not be loaded.
          <button
            type="button"
            onClick={() => void trace.refetch()}
            className="text-primary underline"
          >
            Retry
          </button>
        </div>
      )}
      {boundary && <BoundaryNote boundary={boundary} />}
      {map ? (
        <>
          <p className="text-xs text-muted-foreground">
            Recorded parent links for event #{String(event.event.seq)}
          </p>
          <CausalMap steps={steps} props={props} />
        </>
      ) : (
        <ol
          className="ml-1 flex flex-col gap-1 border-l pl-2"
          data-testid="trace"
        >
          {steps.map((step) => (
            <li key={`${step.runId}:${step.eventId}`}>
              <StepContent
                step={step}
                runId={runId}
                byId={byId}
                current={step.runId === runId && step.eventId === eventId}
                onSelect={onSelect}
                map={false}
              />
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

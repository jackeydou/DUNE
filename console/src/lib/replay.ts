import { parsePayload, type RunEvent } from "@/lib/events"

type Fields = Record<string, unknown>

function fields(value: unknown): Fields | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Fields)
    : undefined
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}

export interface ReplayEvent {
  event: RunEvent
  title: string
  summary: string
  elapsed: number | undefined
  lifecycle: string | undefined
  parentId: string | undefined
  evidenceIds: string[]
  message:
    { sender: string; channel: string | undefined; content: string } | undefined
}

// Appending to the stream leaves the already received event objects intact.
const decoded = new WeakMap<RunEvent, ReplayEvent>()

export function describeEvent(event: RunEvent): ReplayEvent {
  const previous = decoded.get(event)
  if (previous) return previous
  const payload = parsePayload(event)
  const data = fields(payload?.data)
  const metadata = fields(fields(payload?.metadata)?.swarmeval)
  const score = fields(payload?.score)
  const scoreMetadata = fields(fields(score?.metadata)?.swarmeval)
  const evidence = scoreMetadata?.event_ids
  const elapsed = payload?.working_start
  const lifecycle =
    event.type === "swarmeval.lifecycle" ? text(data?.status) : undefined
  const content = data?.content
  const message =
    event.type === "swarmeval.msg.send" && typeof content === "string"
      ? {
          sender: text(data?.sender) ?? (event.agentId || "Run"),
          channel: text(data?.channel),
          content,
        }
      : undefined

  let title = event.type.replace(/^swarmeval\./, "").replaceAll("_", " ")
  let summary = event.line
  switch (event.type) {
    case "model":
      title = text(payload?.model) ?? "Model response"
      summary = event.line.replace(/^model: /, "")
      break
    case "tool":
      title = text(payload?.function) ?? "Tool call"
      if (typeof payload?.result === "string") summary = payload.result
      break
    case "score":
      title = text(payload?.scorer) ?? "Score"
      summary = text(score?.explanation) ?? summary
      break
    case "swarmeval.lifecycle":
      if (lifecycle) title = `Run ${lifecycle}`
      summary = text(data?.error) ?? text(data?.reason) ?? ""
      break
    case "swarmeval.msg.send":
      title = "Message sent"
      summary = message?.content ?? summary
      break
    case "swarmeval.msg.deliver":
      title = "Message delivered"
      summary = text(content) ?? summary
      break
    case "swarmeval.extension":
      title = text(data?.name) ?? title
      break
  }

  const result = {
    event,
    title,
    summary,
    elapsed:
      typeof elapsed === "number" && Number.isFinite(elapsed)
        ? elapsed
        : undefined,
    lifecycle,
    parentId: text(metadata?.parent_id),
    evidenceIds: Array.isArray(evidence)
      ? [
          ...new Set(
            evidence.filter(
              (id): id is string => typeof id === "string" && !!id
            )
          ),
        ]
      : [],
    message,
  }
  decoded.set(event, result)
  return result
}

export function elapsedLabel(event: ReplayEvent): string {
  return event.elapsed === undefined
    ? `#${event.event.seq}`
    : `${event.elapsed >= 0 ? "+" : ""}${event.elapsed.toFixed(1)}s`
}

const PHASES = new Map([
  ["started", "Execution"],
  ["paused", "Paused"],
  ["resumed", "Resumed execution"],
  ["finished", "Results"],
  ["stopped", "Results"],
  ["limit", "Results"],
  ["failed", "Results"],
])

export function replayPhases(events: readonly ReplayEvent[]) {
  const groups: { title: string; events: ReplayEvent[] }[] = []
  let current: (typeof groups)[number] | undefined
  for (const event of events) {
    const phase = event.lifecycle && PHASES.get(event.lifecycle)
    if (!current || phase) {
      current = {
        title:
          phase ||
          (events.some((item) => item.lifecycle === "started")
            ? "Setup"
            : "Events"),
        events: [],
      }
      groups.push(current)
    }
    current.events.push(event)
  }
  return groups
}

export interface TraceStep {
  runId: string
  eventId: string
  seq: bigint
  agentId: string
  line: string
}

export type TraceBoundary =
  | { kind: "source"; runId: string }
  | { kind: "unrecorded" | "missing" | "cycle" }

export function localTrace(
  runId: string,
  byId: ReadonlyMap<string, ReplayEvent>,
  selected: ReplayEvent,
  firstId: string | undefined
): { steps: TraceStep[]; boundary: TraceBoundary | undefined } {
  const steps: TraceStep[] = []
  const seen = new Set<string>()
  let boundary: TraceBoundary | undefined
  let current = selected
  for (;;) {
    const { event, parentId } = current
    if (seen.has(event.eventId)) {
      boundary = { kind: "cycle" }
      break
    }
    seen.add(event.eventId)
    steps.push({
      runId,
      eventId: event.eventId,
      seq: event.seq,
      agentId: event.agentId,
      line: event.line,
    })
    if (!parentId) {
      if (event.eventId !== firstId || event.seq > 1n)
        boundary = { kind: "unrecorded" }
      break
    }
    const separator = parentId.lastIndexOf(":")
    if (separator !== -1) {
      boundary = { kind: "source", runId: parentId.slice(0, separator) }
      break
    }
    const parent = byId.get(parentId)
    if (!parent) {
      boundary = { kind: "missing" }
      break
    }
    current = parent
  }
  return { steps: steps.reverse(), boundary }
}

// A run's events as the replay shows them: laned by agent, filtered by type, scores picked out.
import type { StreamEventsResponse } from "@/gen/swarmeval/api/v1/run_pb"

/** The lane of events no agent caused. */
export const RUN_LANE = "-"

export type RunEvent = StreamEventsResponse

/** Agents in order of first appearance, then the run's own lane. */
export function lanesOf(events: readonly RunEvent[]): string[] {
  const agents: string[] = []
  let runLane = false
  for (const e of events) {
    if (!e.agentId) runLane = true
    else if (!agents.includes(e.agentId)) agents.push(e.agentId)
  }
  return runLane || agents.length === 0 ? [...agents, RUN_LANE] : agents
}

export function typesOf(events: readonly RunEvent[]): string[] {
  return [...new Set(events.map((e) => e.type))].sort()
}

export interface Score {
  scorer: string
  value: string
  explanation: string
  eventId: string
}

/** Each scorer's last score, from the run's `score` events. */
export function scoresOf(events: readonly RunEvent[]): Score[] {
  const last = new Map<string, Score>()
  for (const e of events) {
    if (e.type !== "score") continue
    const payload = parsePayload(e)
    const score = payload?.score as { value?: unknown; explanation?: unknown } | undefined
    const scorer = typeof payload?.scorer === "string" ? payload.scorer : "(unnamed)"
    last.set(scorer, {
      scorer,
      value: score?.value === undefined ? "" : String(score.value),
      explanation: typeof score?.explanation === "string" ? score.explanation : "",
      eventId: e.eventId,
    })
  }
  return [...last.values()]
}

export function parsePayload(event: RunEvent): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(event.payloadJson)
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, unknown>)
      : undefined
  } catch {
    return undefined
  }
}

/** The stored payload, indented, for reading. Text that is not JSON is shown as it is. */
export function prettyPayload(event: RunEvent): string {
  const parsed = parsePayload(event)
  return parsed ? JSON.stringify(parsed, null, 2) : event.payloadJson
}

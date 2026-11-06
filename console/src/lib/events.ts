import type { StreamEventsResponse } from "@/gen/swarmeval/api/v1/run_pb"

export type RunEvent = StreamEventsResponse

/** Agents in order of first appearance, then unattributed run events if present. */
export function agentsOf(events: readonly RunEvent[]): string[] {
  const agents = new Set(events.map((event) => event.agentId))
  if (agents.delete("")) agents.add("")
  return [...agents]
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
    const score = payload?.score as
      { value?: unknown; explanation?: unknown } | undefined
    const scorer =
      typeof payload?.scorer === "string" ? payload.scorer : "(unnamed)"
    last.set(scorer, {
      scorer,
      value: score?.value === undefined ? "" : String(score.value),
      explanation:
        typeof score?.explanation === "string" ? score.explanation : "",
      eventId: e.eventId,
    })
  }
  return [...last.values()]
}

export function parsePayload(
  event: RunEvent
): Record<string, unknown> | undefined {
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

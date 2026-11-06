import { create } from "@bufbuild/protobuf"
import { expect, test } from "vitest"

import { StreamEventsResponseSchema } from "@/gen/swarmeval/api/v1/run_pb"
import {
  describeEvent,
  elapsedLabel,
  localTrace,
  replayPhases,
} from "@/lib/replay"

function item(seq: number, type: string, payload: unknown) {
  return describeEvent(
    create(StreamEventsResponseSchema, {
      seq: BigInt(seq),
      type,
      eventId: `e${seq}`,
      line: "raw line",
      payloadJson: JSON.stringify(payload),
    })
  )
}

test("uses stored time, channel content and scorer references", () => {
  const message = item(2, "swarmeval.msg.send", {
    working_start: 0,
    data: { channel: "returns", sender: "customer", content: "<b>Refund</b>" },
    metadata: { swarmeval: { parent_id: "e1" } },
  })
  expect(elapsedLabel(message)).toBe("+0.0s")
  expect(message.message).toEqual({
    channel: "returns",
    sender: "customer",
    content: "<b>Refund</b>",
  })
  expect(message.parentId).toBe("e1")
  const score = item(3, "score", {
    scorer: "refund",
    score: {
      explanation: "Recorded result",
      metadata: { swarmeval: { event_ids: ["e2", "e2", null] } },
    },
  })
  expect(score).toMatchObject({
    title: "refund",
    summary: "Recorded result",
    evidenceIds: ["e2"],
  })
  expect(elapsedLabel(score)).toBe("#3")
})

test("only recorded lifecycle changes create phases", () => {
  const lifecycle = (seq: number, status: string) =>
    item(seq, "swarmeval.lifecycle", { data: { status } })
  const groups = replayPhases([
    item(1, "sandbox", {}),
    lifecycle(2, "started"),
    item(3, "model", {}),
    lifecycle(4, "paused"),
    lifecycle(5, "resumed"),
    lifecycle(6, "finished"),
    item(7, "score", {}),
  ])
  expect(
    groups.map(({ title, events }) => [
      title,
      events.map(({ event }) => event.eventId),
    ])
  ).toEqual([
    ["Setup", ["e1"]],
    ["Execution", ["e2", "e3"]],
    ["Paused", ["e4"]],
    ["Resumed execution", ["e5"]],
    ["Results", ["e6", "e7"]],
  ])
})

test("follows recorded parents and identifies old, missing, and cross-run boundaries", () => {
  const event = (seq: number, parent_id?: string) =>
    item(seq, "tool", { metadata: { swarmeval: { parent_id } } })
  const root = event(1)
  const child = event(3, "e1")
  const byId = new Map([
    ["e1", root],
    ["e3", child],
  ])
  const trace = localTrace("r", byId, child, "e1")
  expect(trace.steps.map((step) => step.eventId)).toEqual(["e1", "e3"])
  expect(trace.boundary).toBeUndefined()
  expect(localTrace("r", byId, event(4), "e1").boundary).toEqual({
    kind: "unrecorded",
  })
  expect(localTrace("r", byId, event(4, "e2"), "e1").boundary).toEqual({
    kind: "missing",
  })
  expect(localTrace("r", byId, event(4, "parent:e2"), "e1").boundary).toEqual({
    kind: "source",
    runId: "parent",
  })
})

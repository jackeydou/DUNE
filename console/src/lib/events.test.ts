import { create } from "@bufbuild/protobuf"
import { expect, test } from "vitest"

import { StreamEventsResponseSchema } from "@/gen/swarmeval/api/v1/run_pb"
import { lanesOf, prettyPayload, RUN_LANE, scoresOf, typesOf } from "@/lib/events"

function event(seq: number, agentId: string, type: string, payload: unknown) {
  return create(StreamEventsResponseSchema, {
    seq: BigInt(seq),
    eventId: `e${seq}`,
    agentId,
    type,
    payloadJson: typeof payload === "string" ? payload : JSON.stringify(payload),
    line: type,
  })
}

const EVENTS = [
  event(1, "", "swarmeval.lifecycle", { event: "info" }),
  event(2, "qa", "model", { event: "model" }),
  event(3, "dev", "tool", { event: "tool", function: "shell" }),
  event(4, "qa", "model", { event: "model" }),
  event(5, "", "score", { event: "score", scorer: "leak", score: { value: 0, explanation: "first" } }),
  event(6, "", "score", { event: "score", scorer: "leak", score: { value: 1, explanation: "last" } }),
  event(7, "", "score", { event: "score", scorer: "tampered", score: { value: "C" } }),
]

test("lanes are the agents in order of appearance, then the run's own", () => {
  expect(lanesOf(EVENTS)).toEqual(["qa", "dev", RUN_LANE])
  expect(lanesOf(EVENTS.slice(1, 4))).toEqual(["qa", "dev"])
  expect(lanesOf([])).toEqual([RUN_LANE])
})

test("types are listed once, sorted", () => {
  expect(typesOf(EVENTS)).toEqual(["model", "score", "swarmeval.lifecycle", "tool"])
})

test("each scorer's last score is kept", () => {
  expect(scoresOf(EVENTS)).toEqual([
    { scorer: "leak", value: "1", explanation: "last", eventId: "e6" },
    { scorer: "tampered", value: "C", explanation: "", eventId: "e7" },
  ])
})

test("a payload that is not JSON is shown as it is", () => {
  expect(prettyPayload(event(1, "", "x", "<script>not json"))).toBe("<script>not json")
  expect(prettyPayload(event(1, "", "x", { a: 1 }))).toBe('{\n  "a": 1\n}')
  expect(scoresOf([event(1, "", "score", "not json")])[0].scorer).toBe("(unnamed)")
})

import { expect, test } from "vitest"

import { caseGraph } from "@/lib/caseGraph"
import { layout } from "@/lib/flowLayout"

const CASE = `id: c
swarm:
  agents:
    - { id: a, prompt: p.md }
extensions:
  - use: market
    config: { seller: a }
  - use: lonely
scorers:
  - { id: wrote, type: command, sandbox: a }
  - { id: rule, type: rule, meaning: m }
`
const ENV = `sandbox_profiles:
  default: { image: busybox }
`

test("scorers sit in the last column, and a node with no edges in the first", () => {
  const g = caseGraph(new Map([["case.yaml", CASE], ["env.yaml", ENV]]))
  const at = layout(g)
  const x = (id: string) => at.get(id)!.x
  const columns = [...at.values()].map((p) => p.x)
  expect(x("scorer:wrote")).toBe(Math.max(...columns))
  expect(x("scorer:rule")).toBe(Math.max(...columns))
  // The profile reads nothing further, yet sits left of the scorers.
  expect(x("profile:default")).toBeLessThan(x("scorer:wrote"))
  expect(x("extension:lonely:1")).toBe(Math.min(...columns))
  expect(x("extension:market:0")).toBeLessThan(x("agent:a"))
})

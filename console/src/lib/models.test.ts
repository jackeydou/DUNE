import { expect, test } from "vitest"

import { modelsOf, toggled } from "@/lib/models"

test("a run's models are its model.<slot> variant values", () => {
  expect(
    modelsOf({
      framing: "a",
      "model.default": "m1",
      "model.attacker": "m2",
      model: "old",
    })
  ).toEqual({
    default: "m1",
    attacker: "m2",
  })
  expect(modelsOf(undefined)).toEqual({})
})

test("toggling keeps the listed order and leaves other slots alone", () => {
  const listed = ["a", "b", "c"]
  let chosen = toggled({ judge: ["a"] }, "default", "c", true, listed)
  chosen = toggled(chosen, "default", "a", true, listed)
  expect(chosen).toEqual({ judge: ["a"], default: ["a", "c"] })
  expect(toggled(chosen, "default", "a", false, listed)).toEqual({
    judge: ["a"],
    default: ["c"],
  })
})

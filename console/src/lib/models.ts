import type { JsonObject } from "@bufbuild/protobuf"

/** The `variantValues` key prefix of a run's model for a slot: `model.<slot>`
 * (docs/case-format.md#model-slots). */
export const MODEL_ARG = "model."

/** Model slot → the model a run used for it, read from its variant values. */
export function modelsOf(
  values: JsonObject | undefined
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(values ?? {})) {
    if (key.startsWith(MODEL_ARG) && typeof value === "string")
      out[key.slice(MODEL_ARG.length)] = value
  }
  return out
}

/** Toggles `model` in a slot's choice, keeping the order models are listed in. */
export function toggled(
  chosen: Record<string, string[]>,
  slot: string,
  model: string,
  on: boolean,
  listed: string[]
): Record<string, string[]> {
  const now = new Set(chosen[slot] ?? [])
  if (on) now.add(model)
  else now.delete(model)
  return { ...chosen, [slot]: listed.filter((m) => now.has(m)) }
}

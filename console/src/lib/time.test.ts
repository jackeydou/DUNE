import { timestampFromDate } from "@bufbuild/protobuf/wkt"
import { describe, expect, test } from "vitest"

import { ago, duration } from "./time"

const at = (iso: string) => timestampFromDate(new Date(iso))
const now = new Date("2026-10-06T12:00:00Z")

describe("ago", () => {
  test("is relative to now", () => {
    expect(ago(at("2026-10-06T11:57:00Z"), now)).toBe("3 minutes ago")
  })
  test("is a dash for no time", () => {
    expect(ago(undefined, now)).toBe("—")
  })
})

describe("duration", () => {
  test("keeps the two largest units", () => {
    expect(
      duration(at("2026-10-06T10:00:00Z"), at("2026-10-06T11:04:09Z"))
    ).toBe("1h 4m")
    expect(
      duration(at("2026-10-06T10:00:00Z"), at("2026-10-06T10:02:31Z"))
    ).toBe("2m 31s")
  })
  test("skips a zero unit between two others", () => {
    expect(
      duration(at("2026-10-06T10:00:00Z"), at("2026-10-06T11:00:05Z"))
    ).toBe("1h 5s")
  })
  test("runs until now with no end", () => {
    expect(duration(at("2026-10-06T11:59:48Z"), undefined, now)).toBe("12s")
  })
  test("is under a second, or a dash with no start", () => {
    expect(
      duration(at("2026-10-06T10:00:00Z"), at("2026-10-06T10:00:00.400Z"))
    ).toBe("<1s")
    expect(duration(undefined, at("2026-10-06T10:00:00Z"))).toBe("—")
  })
})

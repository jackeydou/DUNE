import { describe, expect, it } from "vitest"

import { base32, newSecret, newSubmissionId, sameSecret } from "./ids"

describe("ids", () => {
  it("encodes RFC 4648 base32 in lower case without padding", () => {
    expect(base32(new TextEncoder().encode("foobar"))).toBe("mzxw6ytboi")
  })

  it("makes submission ids and secrets of fixed shape", () => {
    expect(newSubmissionId()).toMatch(/^sub_[a-z2-7]{26}$/)
    expect(newSecret()).toMatch(/^[a-z2-7]{52}$/)
    expect(newSecret()).not.toBe(newSecret())
  })

  it("compares secrets", () => {
    expect(sameSecret("abc", "abc")).toBe(true)
    expect(sameSecret("abc", "abd")).toBe(false)
    expect(sameSecret("abc", "abcd")).toBe(false)
  })
})

import { create } from "@bufbuild/protobuf"
import { expect, test } from "vitest"

import { CaseFileSchema } from "@/gen/swarmeval/api/v1/case_pb"
import { diffRevisions, parseOverrides, pathProblem, pathsWith, textOf, textsWith, type Changes } from "@/lib/files"

const bytes = (text: string) => new TextEncoder().encode(text)
const file = (path: string, text: string, mode = 0o644) => create(CaseFileSchema, { path, content: bytes(text), mode })

test("text is told from binary", () => {
  expect(textOf(bytes("id: x\n"))).toBe("id: x\n")
  expect(textOf(new Uint8Array([0xff, 0xfe, 0x00]))).toBeUndefined()
  expect(textOf(new Uint8Array([0x61, 0x00, 0x62]))).toBeUndefined()
})

test("the file list follows the pending edits", () => {
  const files = [file("case.yaml", "a"), file("task.md", "b")]
  const changes: Changes = new Map<string, Uint8Array | null>([
    ["task.md", null],
    ["prompts/new.md", bytes("c")],
  ])
  expect(pathsWith(files, changes)).toEqual(["case.yaml", "prompts/new.md"])
})

test("each listed path has its text with the edits, a link its target's, and none for a binary file", () => {
  const files = [
    file("case.yaml", "id: a"),
    file("blob.bin", "x"),
    create(CaseFileSchema, { path: "link", linkTarget: "case.yaml" }),
    file("gone.md", "old"),
  ]
  files[1].content = new Uint8Array([0xff, 0x00])
  const changes: Changes = new Map<string, Uint8Array | null>([
    ["case.yaml", bytes("id: b")],
    ["gone.md", null],
    ["new.md", bytes("")],
  ])
  expect([...textsWith(files, changes)]).toEqual([
    ["blob.bin", undefined],
    ["case.yaml", "id: b"],
    ["link", "id: b"],
    ["new.md", ""],
  ])
})

// Review on #33: a case.yaml or env file that is a link inside the case drew as missing.
test("links are followed inside the case: chains, directory links, and pending edits", () => {
  const link = (path: string, linkTarget: string) => create(CaseFileSchema, { path, linkTarget })
  const files = [
    file("shared/env.yaml", "schema_version: 1"),
    file("shared/case.yaml", "id: real"),
    link("common", "shared"),
    link("case.yaml", "common/case.yaml"),
    link("env.yaml", "./prompts/../shared/env.yaml"),
    link("prompts/up.md", "../case.yaml"),
    link("out.md", "../outside.md"),
    link("abs.md", "/etc/passwd"),
    link("loop_a", "loop_b"),
    link("loop_b", "loop_a"),
    link("dangling.md", "nothing.md"),
    link("to_deleted.md", "shared/env.yaml"),
    link("replaced.md", "shared/case.yaml"),
  ]
  const texts = textsWith(files, new Map())
  expect(texts.get("case.yaml")).toBe("id: real")
  expect(texts.get("env.yaml")).toBe("schema_version: 1")
  expect(texts.get("prompts/up.md")).toBe("id: real")
  for (const path of ["out.md", "abs.md", "loop_a", "dangling.md"]) expect(texts.get(path), path).toBeUndefined()

  const edited = textsWith(
    files,
    new Map<string, Uint8Array | null>([
      ["shared/case.yaml", bytes("id: edited")],
      ["shared/env.yaml", null],
      ["replaced.md", bytes("own text")],
    ])
  )
  expect(edited.get("case.yaml")).toBe("id: edited")
  expect(edited.get("to_deleted.md")).toBeUndefined()
  expect(edited.get("replaced.md")).toBe("own text")
})

test("new paths must be relative and unused", () => {
  expect(pathProblem("prompts/a.md", ["case.yaml"])).toBeUndefined()
  for (const bad of ["", "/abs.md", "../up.md", "a//b.md", "./a.md", "a/../b.md", "case.yaml"])
    expect(pathProblem(bad, ["case.yaml"]), bad).toBeDefined()
})

test("a diff lists added, removed, and changed files, and skips equal ones", () => {
  const from = [file("case.yaml", "id: x\n"), file("task.md", "old\n"), file("gone.md", "bye\n"), file("run.sh", "x\n")]
  const to = [file("case.yaml", "id: x\n"), file("task.md", "new\n"), file("added.md", "hi\n"), file("run.sh", "x\n", 0o755)]
  const diffs = diffRevisions(from, to)
  expect(diffs.map((d) => [d.path, d.kind])).toEqual([
    ["added.md", "added"],
    ["gone.md", "removed"],
    ["run.sh", "changed"],
    ["task.md", "changed"],
  ])
  expect(diffs[3].patch).toContain("-old\n+new")
  expect(diffs[2].patch).toContain("mode 644 → 755")
  expect(diffRevisions(from, from)).toEqual([])
})

test("binary files of one length and different bytes are a change", () => {
  const binary = (bytes: number[]) => create(CaseFileSchema, { path: "data.bin", content: new Uint8Array(bytes), mode: 0o644 })
  const diffs = diffRevisions([binary([0xff, 0x00, 0x01])], [binary([0xff, 0x00, 0x02])])
  expect(diffs.map((d) => [d.path, d.kind])).toEqual([["data.bin", "changed"]])
  expect(diffs[0].patch).toContain("binary content differs")
  expect(diffRevisions([binary([0xff, 0x00, 0x01])], [binary([0xff, 0x00, 0x01])])).toEqual([])
  const link = create(CaseFileSchema, { path: "data.bin", linkTarget: "other.bin", mode: 0o644 })
  const empty = create(CaseFileSchema, { path: "data.bin", mode: 0o644 })
  expect(diffRevisions([link], [empty]).map((d) => d.kind)).toEqual(["changed"])
})

test("overrides are one axis per line, values as JSON when they parse", () => {
  expect(parseOverrides('framing=a,b\n\nn=1,2\nparaphrased=[],["dm_ab"]\n')).toEqual({
    framing: ["a", "b"],
    n: [1, 2],
    paraphrased: [[], ["dm_ab"]],
  })
  expect(parseOverrides("")).toEqual({})
  expect(() => parseOverrides("no equals")).toThrow(/axis=value/)
})

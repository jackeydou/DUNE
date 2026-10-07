import { create } from "@bufbuild/protobuf"
import { expect, test } from "vitest"

import { CaseFileSchema } from "@/gen/swarmeval/api/v1/case_pb"
import { diffRevisions, parseOverrides, pathProblem, pathsWith, textOf, type Changes } from "@/lib/files"

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

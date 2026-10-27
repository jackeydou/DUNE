import { create } from "@bufbuild/protobuf"
import { parse } from "yaml"
import { expect, test } from "vitest"

import { ReviewState, SubmissionFileSchema } from "@/gen/swarmeval/intake/v1/submission_pb"
import { asFileLike, changesOf, filesWith, groupProblems, starter, stateName } from "@/intake/lib"
import { textOf, type Changes } from "@/lib/files"

const bytes = (text: string) => new TextEncoder().encode(text)

test("submission files read as regular 0644 files", () => {
  const files = [create(SubmissionFileSchema, { path: "case.yaml", content: bytes("id: a") })]
  expect(asFileLike(files)).toEqual([{ path: "case.yaml", content: bytes("id: a"), mode: 0o644, linkTarget: "" }])
})

test("a check reads the stored files with the edits applied, sorted", () => {
  const stored = asFileLike([
    create(SubmissionFileSchema, { path: "gone.md", content: bytes("old") }),
    create(SubmissionFileSchema, { path: "case.yaml", content: bytes("a") }),
  ])
  const changes: Changes = new Map<string, Uint8Array | null>([
    ["prompts/b.md", bytes("b")],
    ["gone.md", null],
    ["case.yaml", bytes("a2")],
  ])
  expect(filesWith(stored, changes)).toEqual([
    { path: "case.yaml", content: bytes("a2") },
    { path: "prompts/b.md", content: bytes("b") },
  ])
  expect(filesWith([], changes).map((f) => f.path)).toEqual(["case.yaml", "prompts/b.md"])
})

test("edits become changes: content or a delete", () => {
  const changes: Changes = new Map<string, Uint8Array | null>([
    ["a.md", bytes("x")],
    ["b.md", null],
  ])
  expect(changesOf(changes)).toEqual([
    { path: "a.md", change: { case: "content", value: bytes("x") } },
    { path: "b.md", change: { case: "delete", value: true } },
  ])
})

test("problems group by file, the submission's own first", () => {
  const grouped = groupProblems([
    { path: "prompts/a.md", message: "one" },
    { path: "", message: "whole" },
    { path: "case.yaml", message: "two" },
    { path: "prompts/a.md", message: "three" },
  ])
  expect(grouped).toEqual([
    ["", ["whole"]],
    ["case.yaml", ["two"]],
    ["prompts/a.md", ["one", "three"]],
  ])
})

test("every review state has a name", () => {
  expect(stateName(ReviewState.ACCEPTED)).toBe("accepted")
  expect(stateName(ReviewState.WITHDRAWN)).toBe("withdrawn")
  expect(stateName(ReviewState.UNSPECIFIED)).toBe("unknown")
})

test("the starter keeps to the intake rules", () => {
  const files = starter("my_case", "community")
  expect(files.has("README.md")).toBe(true)
  const text = textOf(files.get("case.yaml")!)!
  const doc = parse(text) as {
    schema_version: number
    id: string
    workspace: string
    extensions?: unknown
    swarm: { agents: { sandbox: string; tools: string[]; prompt: string; task: string }[] }
  }
  expect(doc.schema_version).toBe(5)
  expect(doc).toMatchObject({ id: "my_case", workspace: "community" })
  expect(doc.extensions).toBeUndefined()
  expect(files.has("env.yaml")).toBe(false)
  for (const agent of doc.swarm.agents) {
    expect(agent.sandbox).toBe("none")
    expect(agent.tools).toEqual(["send_message"])
    expect(files.has(agent.prompt) && files.has(agent.task)).toBe(true)
  }
  expect(text).not.toContain("case:")
  expect(text).not.toContain("web_request")
})

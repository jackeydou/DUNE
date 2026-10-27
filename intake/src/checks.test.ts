import { createHash } from "node:crypto"
import { describe, expect, it } from "vitest"

import { checkFiles, contentSha256, type InputFile } from "./checks"
import { MAX_FILE_BYTES, MAX_FILES } from "./config"

const enc = new TextEncoder()
const file = (path: string, content: string | Uint8Array): InputFile => ({ path, content: typeof content === "string" ? enc.encode(content) : content })

const CASE = `schema_version: 5
id: refund_pressure
workspace: community
category: honesty
description: A support agent under pressure to promise a refund.
swarm:
  agents:
    - { id: support, prompt: prompts/support.md, tools: [send_message], sandbox: none }
    - { id: customer, prompt: prompts/customer.md, tools: [send_message], sandbox: none }
  channels:
    - { id: chat, members: [support, customer] }
task:
  input: prompts/task.md
`

function valid(extra: Record<string, string> = {}): InputFile[] {
  const files: Record<string, string> = {
    "case.yaml": CASE,
    "README.md": "# Refund pressure\n",
    "prompts/support.md": "You are support.\n",
    "prompts/customer.md": "You are a customer.\n",
    "prompts/task.md": "Begin.\n",
    ...extra,
  }
  return Object.entries(files).map(([p, c]) => file(p, c))
}

const messages = (files: InputFile[]) => checkFiles(files).problems.map((p) => `${p.path}|${p.message}`)

describe("checkFiles", () => {
  it("accepts a declarative case and keeps what the list shows", () => {
    const checked = checkFiles(valid())
    expect(checked.problems).toEqual([])
    expect(checked.caseId).toBe("refund_pressure")
    expect(checked.category).toBe("honesty")
    expect(checked.description).toMatch(/^A support agent/)
    expect(checked.files.map((f) => f.path)).toEqual(["README.md", "case.yaml", "prompts/customer.md", "prompts/support.md", "prompts/task.md"])
  })

  it("needs case.yaml and README.md", () => {
    expect(messages([file("notes.txt", "x")])).toEqual(
      expect.arrayContaining([expect.stringContaining("README.md"), expect.stringContaining("Add a case.yaml")])
    )
  })

  it("needs the community workspace and a case id", () => {
    const files = valid({ "case.yaml": CASE.replace("workspace: community", "workspace: safety").replace("id: refund_pressure", "id: Refund") })
    const found = messages(files)
    expect(found).toContainEqual(expect.stringContaining("workspace: community"))
    expect(found).toContainEqual(expect.stringContaining("Set `id`"))
  })

  it("refuses web_request, also behind a variant", () => {
    expect(messages(valid({ "case.yaml": CASE.replace("tools: [send_message], sandbox: none }\n    - { id: customer", "tools: [send_message, web_request], sandbox: none }\n    - { id: customer") }))).toContainEqual(
      expect.stringContaining("Agent `support` lists `web_request`")
    )
    const byVariant = CASE.replace("tools: [send_message], sandbox: none }\n    - { id: customer", 'tools: ["${variant.tools}"], sandbox: none }\n    - { id: customer')
    expect(messages(valid({ "case.yaml": byVariant }))).toContainEqual(expect.stringContaining("name each tool literally"))
    const wholeList = CASE.replace("tools: [send_message], sandbox: none }\n    - { id: customer", 'tools: "${variant.tools}", sandbox: none }\n    - { id: customer')
    expect(messages(valid({ "case.yaml": wholeList }))).toContainEqual(expect.stringContaining("list its tools literally"))
  })

  it("refuses case code, also behind a variant", () => {
    const withExt = (use: string) => valid({ "case.yaml": `${CASE}extensions:\n  - use: ${use}\n` })
    expect(messages(withExt("case:extensions/market.py"))).toContainEqual(expect.stringContaining("loads Python from the case"))
    expect(messages(withExt('"${variant.ext}"'))).toContainEqual(expect.stringContaining("`use` literally"))
    expect(messages(withExt("swarmeval.bus.paraphrase"))).toEqual([])
  })

  it("allows only listed images, literally", () => {
    const env = (image: string) => valid({ "env.yaml": `schema_version: 2\nsandbox_profiles:\n  default:\n    image: ${image}\n` })
    expect(messages(env("python:3.12-slim"))).toEqual([])
    expect(messages(env("ghcr.io/someone/tools:latest"))).toContainEqual(expect.stringMatching(/^env\.yaml\|Profile `default` uses image "ghcr.io/))
    expect(messages(env('"${variant.image}"'))).toContainEqual(expect.stringContaining("uses image"))
  })

  it("reads the env file case.yaml names, which must be there", () => {
    const named = CASE + "environment: sandboxes.yaml\n"
    expect(messages(valid({ "case.yaml": named }))).toContainEqual(expect.stringContaining("names sandboxes.yaml, which is not among the files"))
    expect(messages(valid({ "case.yaml": named, "sandboxes.yaml": "sandbox_profiles:\n  default: { image: alpine }\n" }))).toContainEqual(
      expect.stringMatching(/^sandboxes\.yaml\|Profile `default`/)
    )
  })

  it("refuses YAML that does not parse, and alias bombs", () => {
    expect(messages(valid({ "case.yaml": "id: [unclosed\n" }))).toContainEqual(expect.stringContaining("not valid YAML"))
    const bomb = ["a: &a [x, x, x, x, x, x, x, x, x, x]", ...Array.from({ length: 8 }, (_, i) => `${String.fromCharCode(98 + i)}: &${String.fromCharCode(98 + i)} [${Array(10).fill(`*${String.fromCharCode(97 + i)}`).join(", ")}]`)].join("\n")
    expect(messages(valid({ "case.yaml": bomb }))).toContainEqual(expect.stringContaining("case.yaml|"))
  })

  it("takes text only", () => {
    const found = messages(valid({}).concat(file("logo.png", new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0, 0xff])), file("nul.txt", "a\u0000b")))
    expect(found).toContainEqual(expect.stringMatching(/^logo\.png\|.*not UTF-8 text/))
    expect(found).toContainEqual(expect.stringMatching(/^nul\.txt\|.*not UTF-8 text/))
  })

  it("refuses paths that leave the case, repeat, or pass through a file", () => {
    const found = messages(valid().concat(file("../escape.md", "x"), file("/abs.md", "x"), file("a/__pycache__/m.pyc", "x"), file("README.md", "again"), file("prompts/task.md/inner", "x")))
    expect(found).toContainEqual(expect.stringMatching(/^\.\.\/escape\.md\|Use a relative path/))
    expect(found).toContainEqual(expect.stringMatching(/^\/abs\.md\|Use a relative path/))
    expect(found).toContainEqual(expect.stringMatching(/^a\/__pycache__\/m\.pyc\|__pycache__/))
    expect(found).toContainEqual("README.md|The path appears twice.")
    expect(found).toContainEqual("prompts/task.md/inner|The path passes through prompts/task.md, which is a file.")
  })

  it("holds the size limits", () => {
    expect(messages(valid({ "big.md": "x".repeat(MAX_FILE_BYTES + 1) }))).toContainEqual(expect.stringMatching(/^big\.md\|The file is \d+ bytes/))
    const many = valid().concat(Array.from({ length: MAX_FILES }, (_, i) => file(`data/${i}.md`, "x")))
    expect(messages(many)).toContainEqual(expect.stringContaining(`at most ${MAX_FILES} are accepted`))
    const heavy = valid().concat(Array.from({ length: 5 }, (_, i) => file(`data/${i}.md`, "x".repeat(MAX_FILE_BYTES))))
    expect(messages(heavy)).toContainEqual(expect.stringContaining("bytes together"))
  })

  it("refuses a real-looking credential without repeating it", () => {
    const key = "AKIA" + "Q3EGRZ7XN5KVWLMT"
    const found = messages(valid({ "prompts/support.md": `Use this:\n${key}\n` }))
    expect(found).toEqual(["prompts/support.md|Line 2 looks like a real credential (aws-access-token). Remove it; use a canary for a secret the agents should find."])
    expect(found.join("")).not.toContain(key)
  })
})

describe("contentSha256", () => {
  it("hashes path, NUL, byte length, NUL, bytes, in path order", async () => {
    const files = [
      { path: "README.md", content: "é\n" },
      { path: "case.yaml", content: "id: x\n" },
    ]
    const expected = createHash("sha256").update("README.md\u00003\u0000é\ncase.yaml\u00006\u0000id: x\n").digest("hex")
    expect(await contentSha256(files)).toBe(expected)
  })
})

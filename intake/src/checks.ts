// The checks a submission passes before it is stored (spec 2026-10-25-public-case-intake, decision
// 6). They are the intake site's own rules plus what it needs to file a submission: its case id,
// and text it can show. They are not the case loader: whether a case loads is decided by
// `swarmeval.core.load_case` when the maintainers pull it, and nothing here repeats its rules.
import { parseDocument, type Document } from "yaml"

import {
  ALLOWED_IMAGES,
  MAX_FILE_BYTES,
  MAX_FILES,
  MAX_SUMMARY_CHARS,
  MAX_TOTAL_BYTES,
  WORKSPACE,
} from "./config"
import { hex, sha256 } from "./ids"
import { findSecrets } from "./secrets"

export interface InputFile {
  path: string
  content: Uint8Array
}

/** A stored file: its text. */
export interface TextFile {
  path: string
  content: string
}

export interface Problem {
  /** Empty when it is about the submission as a whole. */
  path: string
  message: string
}

export interface Checked {
  problems: Problem[]
  /** Sorted by path. Only meaningful when there are no problems. */
  files: TextFile[]
  caseId: string
  category: string
  description: string
}

// As case-format.md: ids match [a-z][a-z0-9_]*, up to 63 characters.
const CASE_ID = /^[a-z][a-z0-9_]{0,62}$/
// ignoreBOM keeps a byte order mark as text, so what is stored is what was sent.
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })

export function checkFiles(input: readonly InputFile[]): Checked {
  const problems: Problem[] = []
  const add = (path: string, message: string) => problems.push({ path, message })

  if (input.length === 0) add("", "The submission has no files. Add at least case.yaml and README.md.")
  if (input.length > MAX_FILES) add("", `The submission has ${input.length} files; at most ${MAX_FILES} are accepted.`)
  const total = input.reduce((n, f) => n + f.content.length, 0)
  if (total > MAX_TOTAL_BYTES)
    add("", `The files hold ${total} bytes together; at most ${MAX_TOTAL_BYTES} are accepted. Move large data out of the case.`)

  const files: TextFile[] = []
  const seen = new Set<string>()
  for (const file of input) {
    const pathProblem = checkPath(file.path, seen)
    if (pathProblem) {
      add(file.path, pathProblem)
      continue
    }
    seen.add(file.path)
    if (file.content.length > MAX_FILE_BYTES) {
      add(file.path, `The file is ${file.content.length} bytes; a file may be at most ${MAX_FILE_BYTES}.`)
      continue
    }
    const text = decode(file.content)
    if (text === undefined) {
      add(file.path, "The file is not UTF-8 text. Only text files are accepted: no images, archives, or binaries.")
      continue
    }
    for (const hit of findSecrets(text))
      add(file.path, `Line ${hit.line} looks like a real credential (${hit.rule}). Remove it; use a canary for a secret the agents should find.`)
    files.push({ path: file.path, content: text })
  }
  for (const path of seen) {
    const parts = path.split("/")
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/")
      if (seen.has(dir)) add(path, `The path passes through ${dir}, which is a file.`)
    }
  }
  files.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))

  const texts = new Map(files.map((f) => [f.path, f.content]))
  if (!seen.has("README.md")) add("", "Add a README.md at the top: what the case tests, why it matters, and how it is scored.")
  const summary = checkCase(texts, seen, add)
  return { problems, files, ...summary }
}

function checkPath(path: string, seen: ReadonlySet<string>): string | undefined {
  if (!path) return "A file has no path."
  if (path.length > 255) return "The path is longer than 255 characters."
  if (path.startsWith("/") || path.split("/").some((p) => p === "" || p === "." || p === ".."))
    return "Use a relative path with / separators and no . or .. parts."
  if (path.split("/").includes("__pycache__")) return "__pycache__ directories are not accepted."
  if (seen.has(path)) return "The path appears twice."
  return undefined
}

function decode(content: Uint8Array): string | undefined {
  try {
    const text = utf8.decode(content)
    return text.includes("\u0000") ? undefined : text
  } catch {
    return undefined
  }
}

type Add = (path: string, message: string) => void

/** A file's YAML as plain values, or `undefined` after adding a problem for a file that does not
 * parse. Aliases are capped by the library, so a billion-laughs file fails here. */
function parseYaml(path: string, text: string, add: Add): unknown {
  const doc: Document = parseDocument(text)
  if (doc.errors.length > 0) {
    add(path, `The file is not valid YAML: ${doc.errors[0]!.message}`)
    return undefined
  }
  try {
    return doc.toJS({ maxAliasCount: 100 })
  } catch (err) {
    add(path, `The file could not be read as YAML: ${err instanceof Error ? err.message : String(err)}`)
    return undefined
  }
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v)
const isReference = (v: string) => v.includes("${")
const text = (v: unknown) => (typeof v === "string" ? v.slice(0, MAX_SUMMARY_CHARS) : "")

/** case.yaml and its env file: the intake rules, and what the site keeps of them. */
function checkCase(
  texts: ReadonlyMap<string, string>,
  paths: ReadonlySet<string>,
  add: Add
): { caseId: string; category: string; description: string } {
  const none = { caseId: "", category: "", description: "" }
  if (!paths.has("case.yaml")) {
    add("", "Add a case.yaml at the top of the case (docs/case-format.md).")
    return none
  }
  const source = texts.get("case.yaml")
  if (source === undefined) return none
  const doc = parseYaml("case.yaml", source, add)
  if (doc === undefined) return none
  if (!isObject(doc)) {
    add("case.yaml", "case.yaml must be a mapping of keys such as schema_version, id, and swarm.")
    return none
  }

  if (doc.workspace !== WORKSPACE)
    add("case.yaml", `Set \`workspace: ${WORKSPACE}\`. Every submitted case goes into that workspace.`)
  const caseId = typeof doc.id === "string" ? doc.id : ""
  if (!CASE_ID.test(caseId))
    add("case.yaml", "Set `id` to the case's id: a lower-case letter, then lower-case letters, digits, or _, at most 63 characters.")

  const swarm = isObject(doc.swarm) ? doc.swarm : {}
  const agents = Array.isArray(swarm.agents) ? swarm.agents : []
  for (const agent of agents) {
    if (!isObject(agent) || agent.tools === undefined) continue
    const who = typeof agent.id === "string" ? `Agent \`${agent.id}\`` : "An agent"
    if (!Array.isArray(agent.tools)) {
      add("case.yaml", `${who}: list its tools literally; a variant reference is not accepted for \`tools\` here.`)
      continue
    }
    for (const tool of agent.tools) {
      if (typeof tool !== "string" || isReference(tool))
        add("case.yaml", `${who}: name each tool literally; a variant reference is not accepted for a tool here.`)
      else if (tool === "web_request")
        add("case.yaml", `${who} lists \`web_request\`. Submitted cases may not reach the internet; serve what the agent needs from a mock in the sandbox or an extension.`)
    }
  }

  if (doc.extensions !== undefined) {
    if (!Array.isArray(doc.extensions)) add("case.yaml", "List `extensions` literally; a variant reference is not accepted here.")
    else
      for (const ext of doc.extensions) {
        const use = isObject(ext) ? ext.use : undefined
        if (typeof use !== "string" || isReference(use))
          add("case.yaml", "Name each extension's `use` literally; a variant reference is not accepted here.")
        else if (use.startsWith("case:"))
          add("case.yaml", `\`use: ${use}\` loads Python from the case. Submitted cases may use only the platform's built-in extensions.`)
      }
  }

  checkEnv(doc.environment, texts, paths, add)
  return { caseId, category: text(doc.category), description: text(doc.description) }
}

function checkEnv(environment: unknown, texts: ReadonlyMap<string, string>, paths: ReadonlySet<string>, add: Add) {
  if (environment !== undefined && (typeof environment !== "string" || isReference(environment))) {
    add("case.yaml", "Set `environment` to the path of the env file, literally.")
    return
  }
  const path = environment ?? "env.yaml"
  if (!paths.has(path)) {
    if (environment !== undefined) add("case.yaml", `\`environment\` names ${path}, which is not among the files.`)
    return
  }
  const source = texts.get(path)
  if (source === undefined) return
  const env = parseYaml(path, source, add)
  if (env === undefined) return
  const profiles = isObject(env) && isObject(env.sandbox_profiles) ? env.sandbox_profiles : {}
  for (const [name, profile] of Object.entries(profiles)) {
    if (!isObject(profile)) continue
    const image = profile.image
    if (typeof image !== "string" || isReference(image) || !ALLOWED_IMAGES.includes(image))
      add(path, `Profile \`${name}\` uses image ${JSON.stringify(image)}. Submitted cases may use only: ${ALLOWED_IMAGES.join(", ")}.`)
  }
}

/** sha256 of the files in canonical form, as hex: for each file in path order, its path, a NUL,
 * its length in bytes as decimal digits, a NUL, and its bytes. The maintainers' pull recomputes
 * it, so the form is part of the contract (docs/services/intake.md). */
export async function contentSha256(files: readonly TextFile[]): Promise<string> {
  const encoder = new TextEncoder()
  const parts: Uint8Array[] = []
  for (const file of files) {
    const content = encoder.encode(file.content)
    parts.push(encoder.encode(`${file.path}\u0000${content.length}\u0000`), content)
  }
  const joined = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const part of parts) {
    joined.set(part, at)
    at += part.length
  }
  return hex(await sha256(joined))
}

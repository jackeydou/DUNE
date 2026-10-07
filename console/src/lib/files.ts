// A case revision's files in the editor: which are text, what changed, and how two revisions
// differ.
import type { JsonObject, JsonValue } from "@bufbuild/protobuf"
import { createTwoFilesPatch } from "diff"

import type { CaseFile } from "@/gen/swarmeval/api/v1/case_pb"

const utf8 = new TextDecoder("utf-8", { fatal: true })

/** The file's text, or `undefined` for content that is not UTF-8 (shown as binary). */
export function textOf(content: Uint8Array): string | undefined {
  try {
    const text = utf8.decode(content)
    return text.includes("\u0000") ? undefined : text
  } catch {
    return undefined
  }
}

/** A pending edit: new content, or `null` for a delete. */
export type Changes = Map<string, Uint8Array | null>

/** The paths the editor lists: the revision's files with the pending edits applied. */
export function pathsWith(files: readonly CaseFile[], changes: Changes): string[] {
  const paths = new Set(files.map((f) => f.path))
  for (const [path, content] of changes) {
    if (content === null) paths.delete(path)
    else paths.add(path)
  }
  return [...paths].sort()
}

/** Each path the editor lists, with its text, or `undefined` for a binary file or a link. */
export function textsWith(files: readonly CaseFile[], changes: Changes): Map<string, string | undefined> {
  const stored = new Map(files.map((f) => [f.path, f]))
  return new Map(
    pathsWith(files, changes).map((path) => {
      const edited = changes.get(path)
      const file = stored.get(path)
      if (edited) return [path, textOf(edited)]
      return [path, file && !file.linkTarget ? textOf(file.content) : undefined]
    })
  )
}

export function pathProblem(path: string, existing: readonly string[]): string | undefined {
  if (!path) return "Give the file a path."
  if (path.startsWith("/") || path.split("/").some((p) => p === "" || p === "." || p === ".."))
    return "Use a relative path with / separators and no . or .. parts."
  if (existing.includes(path)) return `${path} already exists.`
  return undefined
}

export interface FileDiff {
  path: string
  /** `added`, `removed`, or `changed`. */
  kind: "added" | "removed" | "changed"
  /** A unified diff for text files; a one-line note for binary files and links. */
  patch: string
}

function describe(file: CaseFile): string {
  if (file.linkTarget) return `link to ${file.linkTarget}\n`
  return textOf(file.content) ?? `binary, ${file.content.length} bytes\n`
}

/** Equal by what is stored: the bytes, the link, and the mode. Never by `describe`, which says
 * the same of any two binary files of one length. */
function sameFile(a: CaseFile, b: CaseFile): boolean {
  return (
    a.mode === b.mode &&
    a.linkTarget === b.linkTarget &&
    a.content.length === b.content.length &&
    a.content.every((byte, i) => byte === b.content[i])
  )
}

/** A line for a file whose description is the same on both sides though its content is not. */
function note(a: CaseFile | undefined, b: CaseFile | undefined, left: string, right: string): string {
  return a && b && left === right && a.mode === b.mode ? "binary content differs\n" : ""
}

/** Files that differ between two revisions, by path. */
export function diffRevisions(from: readonly CaseFile[], to: readonly CaseFile[]): FileDiff[] {
  const before = new Map(from.map((f) => [f.path, f]))
  const after = new Map(to.map((f) => [f.path, f]))
  const out: FileDiff[] = []
  for (const path of [...new Set([...before.keys(), ...after.keys()])].sort()) {
    const a = before.get(path)
    const b = after.get(path)
    if (a && b && sameFile(a, b)) continue
    const left = a ? describe(a) : ""
    const right = b ? describe(b) : ""
    const kind = !a ? "added" : !b ? "removed" : "changed"
    const mode = a && b && a.mode !== b.mode ? `mode ${a.mode.toString(8)} → ${b.mode.toString(8)}\n` : ""
    out.push({
      path,
      kind,
      patch: mode + note(a, b, left, right) + createTwoFilesPatch(path, path, left, right, "", "", { context: 3 }),
    })
  }
  return out
}

/** `axis=a,b` lines as the overrides a submission takes; values are read as JSON when they parse. */
export function parseOverrides(text: string): JsonObject {
  const out: JsonObject = {}
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const at = line.indexOf("=")
    if (at <= 0) throw new Error(`"${line}": write one axis per line as axis=value,value`)
    const axis = line.slice(0, at).trim()
    const raw = line.slice(at + 1)
    let values: JsonValue
    try {
      values = JSON.parse(`[${raw}]`) as JsonValue
    } catch {
      values = raw.split(",").map((v) => v.trim())
    }
    out[axis] = values
  }
  return out
}

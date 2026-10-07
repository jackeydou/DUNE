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

// As many links as a path may pass through, as Linux's ELOOP limit.
const MAX_LINKS = 40

/** A link's target as a case path: from the link's directory, `.` and `..` folded, `""` for
 * the case's root. `undefined` for an absolute target or one that leaves the case. */
function linkPath(link: string, target: string): string | undefined {
  if (target.startsWith("/")) return undefined
  const parts = link.split("/").slice(0, -1)
  for (const part of target.split("/")) {
    if (part === "" || part === ".") continue
    if (part !== "..") parts.push(part)
    else if (parts.pop() === undefined) return undefined
  }
  return parts.join("/")
}

/**
 * Each path the editor lists, with its text: a link's is its target's, followed through links
 * in the case, a directory link on the way included, as the loader reads it. `undefined` for a
 * binary file, a link that leaves the case or goes nowhere, and a link chain that loops.
 */
export function textsWith(files: readonly CaseFile[], changes: Changes): Map<string, string | undefined> {
  const stored = new Map(files.map((f) => [f.path, f]))
  // A pending edit replaces a link with content, or deletes it.
  const linkOf = (path: string) => (changes.has(path) ? undefined : stored.get(path)?.linkTarget || undefined)
  const resolve = (path: string): string | undefined => {
    let at = path
    for (let hop = 0; hop <= MAX_LINKS; hop++) {
      const parts = at.split("/")
      const end = parts.findIndex((_, i) => linkOf(parts.slice(0, i + 1).join("/")) !== undefined)
      if (end === -1) return at
      const link = parts.slice(0, end + 1).join("/")
      const landed = linkPath(link, linkOf(link)!)
      if (landed === undefined) return undefined
      at = [landed, ...parts.slice(end + 1)].filter(Boolean).join("/")
    }
    return undefined
  }
  const textAt = (path: string): string | undefined => {
    const edited = changes.get(path)
    if (edited !== undefined) return edited === null ? undefined : textOf(edited)
    const file = stored.get(path)
    return file ? textOf(file.content) : undefined
  }
  return new Map(
    pathsWith(files, changes).map((path) => {
      const target = resolve(path)
      return [path, target ? textAt(target) : undefined]
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

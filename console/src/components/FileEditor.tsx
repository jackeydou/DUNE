import {
  FileSymlink,
  FolderOpen,
  Plus,
  RotateCcw,
  Trash2,
  Upload,
  WrapText,
} from "lucide-react"
import { useState } from "react"

import { CodeEditor, language, type Reveal } from "@/components/CodeEditor"
import { ErrorAlert } from "@/components/common"
import { FileIcon } from "@/components/FileIcon"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { CaseFile } from "@/gen/swarmeval/api/v1/case_pb"
import { pathProblem, pathsWith, textOf, type Changes } from "@/lib/files"
import { cn } from "@/lib/utils"

const encoder = new TextEncoder()

type Row = { kind: "dir"; path: string; name: string; depth: number } | { kind: "file"; path: string; name: string; depth: number }

/** Sorted paths as a tree's rows, folders before the files beside them. */
function treeRows(paths: readonly string[]): Row[] {
  type Dir = { dirs: Map<string, Dir>; files: string[] }
  const root: Dir = { dirs: new Map(), files: [] }
  for (const path of paths) {
    const parts = path.split("/")
    let at = root
    for (const part of parts.slice(0, -1)) {
      if (!at.dirs.has(part)) at.dirs.set(part, { dirs: new Map(), files: [] })
      at = at.dirs.get(part)!
    }
    at.files.push(path)
  }
  const rows: Row[] = []
  const walk = (dir: Dir, prefix: string, depth: number) => {
    for (const [name, sub] of [...dir.dirs].sort(([a], [b]) => a.localeCompare(b))) {
      rows.push({ kind: "dir", path: `${prefix}${name}`, name, depth })
      walk(sub, `${prefix}${name}/`, depth + 1)
    }
    for (const path of dir.files) rows.push({ kind: "file", path, name: path.split("/").at(-1)!, depth })
  }
  walk(root, "", 0)
  return rows
}

function IconButton({ label, onClick, className, children }: { label: string; onClick?: () => void; className?: string; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} onClick={onClick} className={cn("text-muted-foreground", className)}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

/**
 * A revision's files with the reader's pending edits: pick a file, edit text, add, replace a
 * binary file by upload, delete. Nothing is saved here; `onChange` gets the edits so far.
 * `open` is the file, and the lines in it, to show first.
 */
export function FileEditor({
  files,
  changes,
  readOnly,
  open,
  onChange,
}: {
  files: readonly CaseFile[]
  changes: Changes
  readOnly?: boolean
  open?: { path: string; reveal?: Reveal }
  onChange: (next: Changes) => void
}) {
  const paths = pathsWith(files, changes)
  const [selected, setSelected] = useState(open?.path ?? (paths.includes("case.yaml") ? "case.yaml" : paths[0]))
  const [reveal, setReveal] = useState(open?.reveal)
  const [newPath, setNewPath] = useState("")
  const [adding, setAdding] = useState(false)
  const [problem, setProblem] = useState<string>()
  const [wrap, setWrap] = useState(true)
  const [cursor, setCursor] = useState<[number, number]>([1, 1])
  // Bumped when a file's content is replaced from outside the editor, which then starts over.
  const [replaced, setReplaced] = useState(0)
  const stored = new Map(files.map((f) => [f.path, f]))

  const set = (path: string, content: Uint8Array | null) => {
    const next = new Map(changes)
    const original = stored.get(path)
    if (content === null && !original) next.delete(path)
    else next.set(path, content)
    onChange(next)
  }
  const pick = (path: string) => {
    setSelected(path)
    setReveal(undefined)
    setCursor([1, 1])
  }
  const contentOf = (path: string): Uint8Array => changes.get(path) ?? stored.get(path)?.content ?? new Uint8Array()
  const current = selected && paths.includes(selected) ? selected : paths[0]
  const link = current ? (changes.has(current) ? "" : (stored.get(current)?.linkTarget ?? "")) : ""
  const text = current && !link ? textOf(contentOf(current)) : undefined
  const deleted = [...changes].filter(([, c]) => c === null).map(([path]) => path)
  const state = (path: string) => (changes.has(path) ? (stored.has(path) ? "edited" : "new") : undefined)
  const dir = current?.includes("/") ? current.slice(0, current.lastIndexOf("/") + 1) : ""

  return (
    <div className="flex h-[38rem] overflow-hidden rounded-xl border bg-card shadow-xs">
      <aside className="flex w-60 shrink-0 flex-col border-r bg-sidebar">
        <div className="flex h-10 items-center justify-between border-b pr-1.5 pl-3">
          <span className="text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">Files</span>
          {!readOnly && (
            <IconButton label="Add a file" onClick={() => setAdding(!adding)}>
              <Plus />
            </IconButton>
          )}
        </div>
        {!readOnly && adding && (
          <form
            className="flex flex-col gap-2 border-b p-2"
            onSubmit={(e) => {
              e.preventDefault()
              const found = pathProblem(newPath, paths)
              setProblem(found)
              if (found) return
              set(newPath, new Uint8Array())
              pick(newPath)
              setNewPath("")
              setAdding(false)
            }}
          >
            <Input autoFocus aria-label="New file path" placeholder="prompts/new.md" className="h-8 font-mono text-xs" value={newPath} onChange={(e) => setNewPath(e.target.value)} />
            <Button type="submit" variant="outline" size="sm">
              Add file
            </Button>
            <ErrorAlert title="The file was not added" error={problem} />
          </form>
        )}
        <ul className="flex-1 overflow-auto py-1.5 text-[13px]" data-testid="files">
          {treeRows(paths).map((row) =>
            row.kind === "dir" ? (
              <li key={`dir:${row.path}`} className="flex h-7 items-center gap-1.5 pr-2 text-muted-foreground" style={{ paddingLeft: 12 + row.depth * 14 }}>
                <FolderOpen className="size-3.5 shrink-0" />
                <span className="truncate">{row.name}</span>
              </li>
            ) : (
              <li key={row.path}>
                <FileRow row={row} active={row.path === current} state={state(row.path)} onClick={() => pick(row.path)} />
              </li>
            )
          )}
          {deleted.map((path) => (
            <li key={`deleted:${path}`} className="flex h-7 items-center justify-between gap-2 pr-1 pl-3 font-mono text-xs text-muted-foreground">
              <span className="truncate line-through">{path}</span>
              <IconButton
                label={`Restore ${path}`}
                onClick={() => {
                  const next = new Map(changes)
                  next.delete(path)
                  onChange(next)
                }}
              >
                <RotateCcw />
              </IconButton>
            </li>
          ))}
        </ul>
        {changes.size > 0 && (
          <div className="border-t px-3 py-2 text-[11px] text-muted-foreground">
            {changes.size} unsaved change{changes.size === 1 ? "" : "s"}
          </div>
        )}
      </aside>
      <section className="flex min-w-0 flex-1 flex-col">
        {current === undefined ? (
          <p className="p-4 text-sm text-muted-foreground">This case has no files. Add `case.yaml` first.</p>
        ) : (
          <>
            <div className="flex h-10 shrink-0 items-center justify-between gap-2 border-b pr-2 pl-4">
              <div className="flex min-w-0 items-center gap-2">
                {link ? <FileSymlink className="size-4 shrink-0 text-muted-foreground" /> : <FileIcon path={current} className="size-4 shrink-0 text-muted-foreground" />}
                <span className="truncate font-mono text-[13px]" data-testid="current-file">
                  {dir && <span className="text-muted-foreground">{dir}</span>}
                  <span className="font-medium">{current.slice(dir.length)}</span>
                </span>
                {state(current) && <StateMark state={state(current)!} />}
              </div>
              <div className="flex items-center gap-0.5">
                {text !== undefined && (
                  <IconButton label={wrap ? "Don't wrap lines" : "Wrap lines"} onClick={() => setWrap(!wrap)} className={cn(wrap && "bg-accent text-foreground")}>
                    <WrapText />
                  </IconButton>
                )}
                {!readOnly && (
                  <>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button asChild variant="ghost" size="icon-sm" className="text-muted-foreground">
                          <label className="cursor-pointer" aria-label="Replace with a file">
                            <Upload />
                            <input
                              type="file"
                              className="sr-only"
                              onChange={async (e) => {
                                const file = e.target.files?.[0]
                                if (file) {
                                  set(current, new Uint8Array(await file.arrayBuffer()))
                                  setReplaced(replaced + 1)
                                }
                                e.target.value = ""
                              }}
                            />
                          </label>
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Replace with a file</TooltipContent>
                    </Tooltip>
                    <IconButton label="Delete file" onClick={() => set(current, null)} className="hover:bg-danger/10 hover:text-danger">
                      <Trash2 />
                    </IconButton>
                  </>
                )}
              </div>
            </div>
            <div className="min-h-0 flex-1">
              {link ? (
                <p className="p-4 text-sm text-muted-foreground">
                  A symbolic link to <span className="font-mono">{link}</span>. Replace it with a file, or delete it.
                </p>
              ) : text === undefined ? (
                <p className="p-4 text-sm text-muted-foreground">A binary file of {contentOf(current).length} bytes. Replace it with an upload, or delete it.</p>
              ) : (
                <CodeEditor
                  key={`${current}:${replaced}`}
                  className="h-full"
                  path={current}
                  value={text}
                  readOnly={readOnly}
                  wrap={wrap}
                  reveal={reveal}
                  onChange={(next) => set(current, encoder.encode(next))}
                  onCursor={(line, column) => setCursor([line, column])}
                />
              )}
            </div>
            <footer className="flex h-7 shrink-0 items-center gap-4 border-t bg-sidebar px-4 text-[11px] text-muted-foreground">
              <span>{link ? "Link" : text === undefined ? "Binary" : language(current).name}</span>
              {text !== undefined && (
                <>
                  <span>
                    Ln {cursor[0]}, Col {cursor[1]}
                  </span>
                  <span>{text.split("\n").length} lines</span>
                  <span>UTF-8</span>
                </>
              )}
              <span className="ml-auto">{readOnly ? "Read-only" : state(current) ? "Unsaved" : "Saved"}</span>
            </footer>
          </>
        )}
      </section>
    </div>
  )
}

function StateMark({ state }: { state: "edited" | "new" }) {
  return (
    <span title={state} className={cn("font-mono text-[11px] font-semibold", state === "new" ? "text-success" : "text-warning")}>
      {state === "new" ? "A" : "M"}
    </span>
  )
}

function FileRow({ row, active, state, onClick }: { row: Row; active: boolean; state?: "edited" | "new"; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={row.path}
      onClick={onClick}
      style={{ paddingLeft: 12 + row.depth * 14 }}
      className={cn(
        "relative flex h-7 w-full items-center gap-1.5 pr-3 text-left transition-colors hover:bg-sidebar-accent",
        active ? "bg-sidebar-accent font-medium text-foreground before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-primary" : "text-sidebar-foreground"
      )}
    >
      <FileIcon path={row.path} className={cn("size-3.5 shrink-0", active ? "text-primary" : "text-muted-foreground")} />
      <span className="flex-1 truncate">{row.name}</span>
      {state && <StateMark state={state} />}
    </button>
  )
}

import { useState } from "react"

import { CodeEditor } from "@/components/CodeEditor"
import { ErrorAlert } from "@/components/common"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { CaseFile } from "@/gen/swarmeval/api/v1/case_pb"
import { pathProblem, pathsWith, textOf, type Changes } from "@/lib/files"

const encoder = new TextEncoder()

/** A revision's files with the reader's pending edits: pick a file, edit text, add, replace a
 * binary file by upload, delete. Nothing is saved here; `onChange` gets the edits so far. */
export function FileEditor({ files, changes, readOnly, onChange }: { files: readonly CaseFile[]; changes: Changes; readOnly?: boolean; onChange: (next: Changes) => void }) {
  const paths = pathsWith(files, changes)
  const [selected, setSelected] = useState(paths.includes("case.yaml") ? "case.yaml" : paths[0])
  const [newPath, setNewPath] = useState("")
  const [problem, setProblem] = useState<string>()
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
  const contentOf = (path: string): Uint8Array => changes.get(path) ?? stored.get(path)?.content ?? new Uint8Array()
  const current = selected && paths.includes(selected) ? selected : paths[0]
  const link = current ? (changes.has(current) ? "" : (stored.get(current)?.linkTarget ?? "")) : ""
  const text = current && !link ? textOf(contentOf(current)) : undefined

  return (
    <div className="grid grid-cols-[16rem_1fr] gap-4">
      <div className="flex flex-col gap-2">
        <ul className="flex flex-col text-sm" data-testid="files">
          {paths.map((path) => (
            <li key={path}>
              <button
                type="button"
                onClick={() => setSelected(path)}
                className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1 text-left font-mono text-xs hover:bg-muted ${path === current ? "bg-muted" : ""}`}
              >
                <span className="truncate">{path}</span>
                {changes.has(path) && <Badge variant="secondary">{stored.has(path) ? "edited" : "new"}</Badge>}
              </button>
            </li>
          ))}
          {[...changes].filter(([, c]) => c === null).map(([path]) => (
            <li key={path} className="flex items-center justify-between gap-2 px-2 py-1 font-mono text-xs text-muted-foreground line-through">
              <span className="truncate">{path}</span>
              <Button size="xs" variant="ghost" onClick={() => { const next = new Map(changes); next.delete(path); onChange(next) }}>
                Restore
              </Button>
            </li>
          ))}
        </ul>
        {!readOnly && (
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              const found = pathProblem(newPath, paths)
              setProblem(found)
              if (found) return
              set(newPath, new Uint8Array())
              setSelected(newPath)
              setNewPath("")
            }}
          >
            <Input aria-label="New file path" placeholder="prompts/new.md" className="font-mono text-xs" value={newPath} onChange={(e) => setNewPath(e.target.value)} />
            <Button type="submit" variant="outline" size="sm">Add file</Button>
            <ErrorAlert title="The file was not added" error={problem} />
          </form>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-2">
        {current === undefined ? (
          <p className="text-sm text-muted-foreground">This case has no files. Add `case.yaml` first.</p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm" data-testid="current-file">{current}</span>
              {!readOnly && (
                <div className="flex items-center gap-2">
                  <label className="text-sm text-muted-foreground">
                    Replace with a file{" "}
                    <input
                      type="file"
                      className="text-xs"
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
                  <Button variant="destructive" size="sm" onClick={() => set(current, null)}>Delete file</Button>
                </div>
              )}
            </div>
            {link ? (
              <p className="text-sm text-muted-foreground">A symbolic link to <span className="font-mono">{link}</span>. Replace it with a file, or delete it.</p>
            ) : text === undefined ? (
              <p className="text-sm text-muted-foreground">A binary file of {contentOf(current).length} bytes. Replace it with an upload, or delete it.</p>
            ) : (
              <CodeEditor key={`${current}:${replaced}`} path={current} value={text} readOnly={readOnly} onChange={(next) => set(current, encoder.encode(next))} />
            )}
          </>
        )}
      </div>
    </div>
  )
}

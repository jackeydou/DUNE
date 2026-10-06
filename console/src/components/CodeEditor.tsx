import { markdown } from "@codemirror/lang-markdown"
import { python } from "@codemirror/lang-python"
import { yaml } from "@codemirror/lang-yaml"
import { EditorState, type Extension } from "@codemirror/state"
import { EditorView } from "@codemirror/view"
import { basicSetup } from "codemirror"
import { useEffect, useRef } from "react"

function language(path: string): Extension[] {
  if (/\.ya?ml$/.test(path)) return [yaml()]
  if (path.endsWith(".py")) return [python()]
  if (path.endsWith(".md")) return [markdown()]
  return []
}

/** A text editor for one file. `path` picks the language and, when it changes, resets the
 * editor to `value`; while it stays, the editor owns the text and reports each change. */
export function CodeEditor({ path, value, readOnly, onChange }: { path: string; value: string; readOnly?: boolean; onChange: (text: string) => void }) {
  const host = useRef<HTMLDivElement>(null)
  const latest = useRef(onChange)
  useEffect(() => {
    latest.current = onChange
  })
  useEffect(() => {
    if (!host.current) return
    const view = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          ...language(path),
          EditorState.readOnly.of(readOnly === true),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) latest.current(update.state.doc.toString())
          }),
          EditorView.theme({
            "&": { fontSize: "13px", height: "100%", backgroundColor: "var(--card)" },
            ".cm-scroller": { overflow: "auto", fontFamily: "var(--font-mono)" },
            ".cm-gutters": { backgroundColor: "var(--muted)", color: "var(--muted-foreground)", borderRight: "1px solid var(--border)" },
            ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "color-mix(in oklch, var(--accent) 60%, transparent)" },
            "&.cm-focused": { outline: "none" },
          }),
        ],
      }),
    })
    return () => view.destroy()
    // `value` is the starting text only: the editor is rebuilt when the file changes, not on
    // every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, readOnly])
  return <div ref={host} data-testid="editor" className="h-[32rem] overflow-hidden" />
}

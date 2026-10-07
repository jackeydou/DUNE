import { markdown } from "@codemirror/lang-markdown"
import { python } from "@codemirror/lang-python"
import { yaml } from "@codemirror/lang-yaml"
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language"
import { Compartment, EditorSelection, EditorState, type Extension } from "@codemirror/state"
import { EditorView, lineNumbers } from "@codemirror/view"
import { tags as t } from "@lezer/highlight"
import { basicSetup } from "codemirror"
import { useEffect, useRef } from "react"

import { cn } from "@/lib/utils"

/** The language's name for the status bar, and its CodeMirror support. */
export function language(path: string): { name: string; extensions: Extension[] } {
  if (/\.ya?ml$/.test(path)) return { name: "YAML", extensions: [yaml()] }
  if (path.endsWith(".py")) return { name: "Python", extensions: [python()] }
  if (path.endsWith(".md")) return { name: "Markdown", extensions: [markdown()] }
  if (path.endsWith(".json")) return { name: "JSON", extensions: [] }
  return { name: "Plain text", extensions: [] }
}

const mix = (token: string, pct: number) => `color-mix(in oklch, var(${token}) ${pct}%, transparent)`

// Colours from the theme's tokens, so the editor follows it.
const highlight = HighlightStyle.define([
  { tag: [t.propertyName, t.definition(t.propertyName)], color: "var(--primary)" },
  { tag: [t.string, t.special(t.string)], color: "var(--success)" },
  { tag: [t.number, t.bool, t.null, t.atom], color: "var(--chart-2)" },
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.modifier], color: "var(--chart-2)", fontWeight: "500" },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.className], color: "var(--info)" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "var(--muted-foreground)", fontStyle: "italic" },
  { tag: [t.meta, t.punctuation, t.separator, t.bracket, t.processingInstruction], color: "var(--muted-foreground)" },
  { tag: t.heading, color: "var(--foreground)", fontWeight: "700" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strong, fontWeight: "700" },
  { tag: [t.link, t.url], color: "var(--info)", textDecoration: "underline" },
  { tag: t.monospace, color: "var(--primary)" },
  { tag: [t.labelName, t.typeName, t.tagName], color: "var(--warning)" },
  { tag: t.invalid, color: "var(--danger)" },
])

const theme = EditorView.theme({
  "&": { fontSize: "13px", height: "100%", backgroundColor: "var(--card)", color: "var(--foreground)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { overflow: "auto", fontFamily: "var(--font-mono)", lineHeight: "1.65" },
  ".cm-content": { padding: "12px 0", caretColor: "var(--primary)" },
  ".cm-cursor, .cm-dropCursor": { borderLeft: "2px solid var(--primary)" },
  ".cm-gutters": { backgroundColor: "var(--card)", color: mix("--muted-foreground", 70), border: "none" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 10px 0 18px", minWidth: "4ch" },
  ".cm-foldGutter .cm-gutterElement": { padding: "0 6px 0 0" },
  ".cm-activeLine": { backgroundColor: mix("--primary", 5) },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--foreground)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: mix("--primary", 18) },
  "&.cm-focused .cm-matchingBracket": { backgroundColor: mix("--primary", 16), outline: "none" },
  ".cm-searchMatch": { backgroundColor: mix("--warning", 28), outline: "none" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: mix("--warning", 50) },
  ".cm-selectionMatch": { backgroundColor: mix("--info", 14) },
  ".cm-foldPlaceholder": { backgroundColor: "var(--muted)", border: "none", color: "var(--muted-foreground)", padding: "0 6px", borderRadius: "4px" },
  ".cm-tooltip": { backgroundColor: "var(--popover)", color: "var(--popover-foreground)", border: "1px solid var(--border)", borderRadius: "8px", boxShadow: "var(--shadow-md)", overflow: "hidden" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--accent)", color: "var(--accent-foreground)" },
  ".cm-panels": { backgroundColor: "var(--muted)", color: "var(--foreground)" },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--border)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
  ".cm-panel.cm-search": { padding: "8px 12px", fontFamily: "var(--font-sans)", fontSize: "12px" },
  ".cm-textfield": { border: "1px solid var(--input)", borderRadius: "6px", backgroundColor: "var(--card)", padding: "3px 8px", fontSize: "12px" },
  ".cm-button": { backgroundImage: "none", backgroundColor: "var(--card)", border: "1px solid var(--border)", borderRadius: "6px", padding: "3px 10px", fontSize: "12px" },
  ".cm-panel.cm-search [name=close]": { fontSize: "16px", color: "var(--muted-foreground)", right: "8px" },
})

/** The theme and syntax colours every editor and viewer here shares. */
const look: Extension[] = [theme, syntaxHighlighting(highlight)]

/** The lines to select when the editor opens: `line` (1-based) and the `lines - 1` after it. */
export interface Reveal {
  line: number
  lines?: number
}

/**
 * A text editor for one file. `path` picks the language and, when it changes, resets the editor
 * to `value`; while it stays, the editor owns the text and reports each change. `reveal` is read
 * once, when the editor opens.
 */
export function CodeEditor({
  path,
  value,
  readOnly,
  wrap,
  reveal,
  className,
  onChange,
  onCursor,
}: {
  path: string
  value: string
  readOnly?: boolean
  wrap?: boolean
  reveal?: Reveal
  className?: string
  onChange: (text: string) => void
  onCursor?: (line: number, column: number) => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView>(null)
  const wrapping = useRef(new Compartment())
  const callbacks = useRef({ onChange, onCursor })
  useEffect(() => {
    callbacks.current = { onChange, onCursor }
  })
  useEffect(() => {
    if (!host.current) return
    const created = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          ...language(path).extensions,
          ...look,
          wrapping.current.of(wrap ? EditorView.lineWrapping : []),
          EditorState.readOnly.of(readOnly === true),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) callbacks.current.onChange(update.state.doc.toString())
            if (update.docChanged || update.selectionSet) {
              const head = update.state.selection.main.head
              const line = update.state.doc.lineAt(head)
              callbacks.current.onCursor?.(line.number, head - line.from + 1)
            }
          }),
        ],
      }),
    })
    view.current = created
    if (reveal) {
      const doc = created.state.doc
      const first = doc.line(Math.min(Math.max(reveal.line, 1), doc.lines))
      const last = doc.line(Math.min(first.number + (reveal.lines ?? 1) - 1, doc.lines))
      created.dispatch({
        selection: EditorSelection.single(first.from, last.to),
        effects: EditorView.scrollIntoView(first.from, { y: "center" }),
      })
      created.focus()
    }
    return () => {
      created.destroy()
      view.current = null
    }
    // `value`, `wrap`, and `reveal` are where the editor starts: it is rebuilt when the file
    // changes, not on every keystroke, and `wrap` is reconfigured below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, readOnly])
  useEffect(() => {
    view.current?.dispatch({ effects: wrapping.current.reconfigure(wrap ? EditorView.lineWrapping : []) })
  }, [wrap])
  return <div ref={host} data-testid="editor" className={cn("h-[32rem] overflow-hidden", className)} />
}

/** Read-only text with line numbers from `firstLine`, as tall as its content. */
export function CodeView({ path, text, firstLine = 1, className }: { path: string; text: string; firstLine?: number; className?: string }) {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!host.current) return
    const created = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: text.replace(/\n$/, ""),
        extensions: [
          lineNumbers({ formatNumber: (n) => String(n + firstLine - 1) }),
          ...language(path).extensions,
          ...look,
          EditorView.lineWrapping,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.theme({
            "&": { fontSize: "12px", backgroundColor: "transparent" },
            ".cm-gutters": { backgroundColor: "transparent" },
            ".cm-content": { padding: "8px 0" },
            ".cm-lineNumbers .cm-gutterElement": { padding: "0 8px 0 10px", minWidth: "3ch" },
          }),
        ],
      }),
    })
    return () => created.destroy()
  }, [path, text, firstLine])
  return <div ref={host} className={cn("overflow-auto", className)} />
}

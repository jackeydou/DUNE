import { ArrowUpRight, X } from "lucide-react"
import type { ReactNode } from "react"

import { CodeView, type Reveal } from "@/components/CodeEditor"
import { FileIcon } from "@/components/FileIcon"
import { KINDS, KIND_ORDER, RELATIONS } from "@/components/flowKinds"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { CaseGraph, CaseNode } from "@/lib/caseGraph"

function Section({ title, action, children }: { title: ReactNode; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2 border-t px-4 py-3.5 first:border-t-0">
      <div className="flex min-h-6 items-center justify-between gap-2">
        <h3 className="text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
      {rows.map(([name, value], i) => (
        <div key={i} className="contents">
          <dt className="text-muted-foreground">{name}</dt>
          <dd className="font-mono break-words whitespace-pre-wrap">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** A node named by a chip in its kind's colour; clicking it selects the node. */
function NodeChip({ node, onSelect }: { node: CaseNode; onSelect: (id: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(node.id)}
      className="inline-flex max-w-full items-center gap-1.5 rounded-md border bg-card px-2 py-0.5 font-mono text-xs transition-colors hover:bg-accent"
    >
      <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: KINDS[node.kind].color }} />
      <span className="truncate">{node.title}</span>
    </button>
  )
}

/** The case as a whole, shown while no node is selected. */
export function CaseOverview({ graph, onSelect }: { graph: CaseGraph; onSelect: (id: string) => void }) {
  const s = graph.summary
  return (
    <div className="flex flex-col">
      <div className="px-4 pt-4 pb-3">
        <div className="text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">Case</div>
        <div className="mt-0.5 font-mono text-base font-semibold">{s.id || "—"}</div>
        {s.description && <p className="mt-2 text-sm leading-relaxed whitespace-pre-wrap text-foreground/85">{s.description}</p>}
      </div>
      <Section title="Run">
        <Rows
          rows={[
            ["workspace", s.workspace || "—"],
            ["category", s.category || "—"],
            ["variants", `${s.variants} per choice of models`],
            ["epochs", `${s.epochs} per variant`],
            ["turn policy", s.turnPolicy],
            ["model slots", s.modelSlots.join(", ") || "—"],
            ...s.limits,
            ["environment", s.environment],
          ]}
        />
      </Section>
      <Section title="Parts">
        <div className="flex flex-col gap-2.5">
          {KIND_ORDER.map((kind) => {
            const of = graph.nodes.filter((n) => n.kind === kind)
            if (!of.length) return null
            const Icon = KINDS[kind].icon
            return (
              <div key={kind} className="flex flex-col gap-1.5">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Icon className="size-3.5" style={{ color: KINDS[kind].color }} />
                  {KINDS[kind].plural}
                  <span className="text-muted-foreground/70">{of.length}</span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {of.map((n) => (
                    <NodeChip key={n.id} node={n} onSelect={onSelect} />
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </Section>
      <p className="border-t px-4 py-3 text-xs text-muted-foreground">Select a part to see its settings and the YAML it is written as.</p>
    </div>
  )
}

function OpenButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button variant="ghost" size="xs" className="text-muted-foreground hover:text-foreground" onClick={onClick}>
      {label} <ArrowUpRight />
    </Button>
  )
}

/** One node: its settings, what it connects to, its YAML, and the files it points to. */
export function NodeDetail({
  graph,
  node,
  texts,
  onSelect,
  onOpen,
  onClose,
}: {
  graph: CaseGraph
  node: CaseNode
  texts: ReadonlyMap<string, string | undefined>
  onSelect: (id: string) => void
  onOpen: (path: string, reveal?: Reveal) => void
  onClose: () => void
}) {
  const kind = KINDS[node.kind]
  const Icon = kind.icon
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const relations = new Map<string, CaseNode[]>()
  for (const e of graph.edges) {
    const [phrase, other] = e.from === node.id ? [RELATIONS[e.kind].out, e.to] : e.to === node.id ? [RELATIONS[e.kind].in, e.from] : []
    if (!phrase || !other) continue
    relations.set(phrase, [...(relations.get(phrase) ?? []), byId.get(other)!])
  }
  const source = node.source
  return (
    <div className="flex flex-col" data-testid="node-detail">
      <div className="flex items-start gap-3 px-4 pt-4 pb-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg" style={{ backgroundColor: `color-mix(in oklch, ${kind.color} 12%, transparent)`, color: kind.color }}>
          <Icon className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">{kind.label}</div>
          <div className="truncate font-mono text-base font-semibold" title={node.title}>
            {node.title}
          </div>
          {node.subtitle && <div className="truncate text-xs text-muted-foreground">{node.subtitle}</div>}
        </div>
        <Button variant="ghost" size="icon-sm" aria-label="Close" className="-mt-1 -mr-2 text-muted-foreground" onClick={onClose}>
          <X />
        </Button>
      </div>
      {node.fields.length > 0 && (
        <Section title="Settings">
          <Rows rows={node.fields} />
        </Section>
      )}
      {relations.size > 0 && (
        <Section title="Connections">
          <div className="flex flex-col gap-2">
            {[...relations].map(([phrase, others]) => (
              <div key={phrase} className="grid grid-cols-[6.5rem_minmax(0,1fr)] items-start gap-x-3 text-xs">
                <span className="pt-0.5 text-muted-foreground">{phrase}</span>
                <div className="flex flex-wrap gap-1.5">
                  {others.map((o) => (
                    <NodeChip key={o.id} node={o} onSelect={onSelect} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Section>
      )}
      {source && (
        <Section
          title={
            <span className="normal-case">
              <span className="font-mono tracking-normal">{source.path}</span>
              <span className="font-normal"> · line {source.line}</span>
            </span>
          }
          action={<OpenButton label="Edit" onClick={() => onOpen(source.path, { line: source.line, lines: source.text.split("\n").length })} />}
        >
          <div className="overflow-hidden rounded-lg border bg-muted/30">
            <CodeView path={source.path} text={source.text} firstLine={source.line} className="max-h-72" />
          </div>
        </Section>
      )}
      {node.files.map((path) => {
        const text = texts.get(path)
        return (
          <Section
            key={path}
            title={
              <span className="flex items-center gap-1.5 normal-case">
                <FileIcon path={path} className="size-3.5" />
                <span className="font-mono tracking-normal">{path}</span>
              </span>
            }
            action={texts.has(path) ? <OpenButton label="Open" onClick={() => onOpen(path)} /> : <Badge variant="outline">not in the case</Badge>}
          >
            {!texts.has(path) ? null : text === undefined ? (
              <p className="text-xs text-muted-foreground">A binary file or a link.</p>
            ) : text ? (
                <div className="overflow-hidden rounded-lg border bg-muted/30">
                  <CodeView path={path} text={text} className="max-h-64" />
                </div>
              ) : (
              <p className="text-xs text-muted-foreground">Empty.</p>
            )}
          </Section>
        )
      })}
    </div>
  )
}

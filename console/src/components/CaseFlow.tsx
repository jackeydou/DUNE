import "@xyflow/react/dist/base.css"

import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react"
import { FileWarning, Maximize, Minus, Plus } from "lucide-react"
import { useMemo, useState } from "react"

import { CaseOverview, NodeDetail } from "@/components/CaseNodeDetail"
import type { Reveal } from "@/components/CodeEditor"
import { KINDS, KIND_ORDER, RELATIONS } from "@/components/flowKinds"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { caseGraph, YamlProblem, type CaseGraph, type CaseNode } from "@/lib/caseGraph"
import { layout, NODE_WIDTH, nodeHeight } from "@/lib/flowLayout"
import { cn } from "@/lib/utils"

type CardData = { node: CaseNode; dim: boolean }
type CardNode = Node<CardData, "card">

const MAX_CHIPS = 3

function Card({ data, selected }: NodeProps<CardNode>) {
  const { node, dim } = data
  const kind = KINDS[node.kind]
  const Icon = kind.icon
  const extra = node.chips.length - MAX_CHIPS
  return (
    <div
      data-testid="flow-node"
      style={{ width: NODE_WIDTH, height: nodeHeight(node), "--kind": kind.color } as React.CSSProperties}
      className={cn(
        "group flex cursor-pointer flex-col justify-center gap-1.5 overflow-hidden rounded-lg border bg-card px-3 shadow-xs transition-[opacity,box-shadow,border-color] duration-150",
        "hover:border-(--kind) hover:shadow-md",
        selected && "border-(--kind) shadow-md ring-2 ring-(--kind)/25",
        dim && "opacity-35"
      )}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="!size-1.5 !min-h-0 !min-w-0 !border-0 !bg-(--kind) opacity-60" />
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-(--kind)/12 text-(--kind)">
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[10px] leading-tight font-medium tracking-wider text-muted-foreground uppercase">{kind.label}</div>
          <div className="truncate font-mono text-[13px] leading-snug font-semibold text-foreground" title={node.title}>
            {node.title}
          </div>
        </div>
      </div>
      {node.chips.length > 0 && (
        <div className="flex min-w-0 items-center gap-1">
          {node.chips.slice(0, MAX_CHIPS).map((chip, i) => (
            <span key={i} className="min-w-0 truncate rounded-sm border bg-muted/60 px-1.5 py-px font-mono text-[10px] leading-4 text-muted-foreground" title={chip}>
              {chip}
            </span>
          ))}
          {extra > 0 && <span className="shrink-0 text-[10px] text-muted-foreground">+{extra}</span>}
        </div>
      )}
      <Handle type="source" position={Position.Right} isConnectable={false} className="!size-1.5 !min-h-0 !min-w-0 !border-0 !bg-(--kind) opacity-60" />
    </div>
  )
}

const nodeTypes = { card: Card }

function ZoomButton({ label, onClick, children }: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} onClick={onClick} className="text-muted-foreground">
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  )
}

function Canvas({ graph, texts, onOpen }: { graph: CaseGraph; texts: ReadonlyMap<string, string | undefined>; onOpen: (path: string, reveal?: Reveal) => void }) {
  const flow = useReactFlow()
  const [selected, setSelected] = useState<string>()
  const [hovered, setHovered] = useState<string>()
  const positions = useMemo(() => layout(graph), [graph])
  const chosen = graph.nodes.find((n) => n.id === selected)
  const focus = hovered ?? chosen?.id

  const near = useMemo(() => {
    if (!focus) return undefined
    const ids = new Set([focus])
    for (const e of graph.edges) {
      if (e.from === focus) ids.add(e.to)
      if (e.to === focus) ids.add(e.from)
    }
    return ids
  }, [graph, focus])

  const nodes: CardNode[] = useMemo(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: "card",
        position: positions.get(node.id) ?? { x: 0, y: 0 },
        width: NODE_WIDTH,
        height: nodeHeight(node),
        data: { node, dim: !!near && !near.has(node.id) },
        selected: node.id === chosen?.id,
        draggable: false,
        connectable: false,
        ariaLabel: `${KINDS[node.kind].label} ${node.title}`,
      })),
    [graph, positions, near, chosen]
  )

  const edges: Edge[] = useMemo(() => {
    const focused = focus ? graph.nodes.find((n) => n.id === focus) : undefined
    return graph.edges.map((e) => {
      const relation = RELATIONS[e.kind]
      const lit = !!focus && (e.from === focus || e.to === focus)
      const color = lit && focused ? KINDS[focused.kind].color : "var(--xy-edge-stroke)"
      return {
        id: `${e.from}->${e.to}`,
        source: e.from,
        target: e.to,
        focusable: false,
        selectable: false,
        style: {
          stroke: color,
          strokeWidth: lit ? 2 : 1.25,
          strokeDasharray: relation.dash,
          opacity: focus && !lit ? 0.2 : 1,
        },
        markerEnd: relation.arrow ? { type: MarkerType.ArrowClosed, width: 14, height: 14, color } : undefined,
        zIndex: lit ? 1 : 0,
      }
    })
  }, [graph, focus])

  const select = (id: string) => {
    setSelected(id)
    const at = positions.get(id)
    const node = graph.nodes.find((n) => n.id === id)
    if (at && node) void flow.setCenter(at.x + NODE_WIDTH / 2, at.y + nodeHeight(node) / 2, { zoom: Math.max(flow.getZoom(), 0.9), duration: 400 })
  }

  const kinds = KIND_ORDER.filter((k) => graph.nodes.some((n) => n.kind === k))

  return (
    <div className="grid overflow-hidden rounded-xl border bg-card shadow-xs lg:grid-cols-[minmax(0,1fr)_24rem]">
      <div className="relative h-[38rem] bg-background/60" data-testid="flow">
        <ReactFlow<CardNode>
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={(changes: NodeChange<CardNode>[]) => {
            for (const c of changes) {
              if (c.type !== "select") continue
              if (c.selected) setSelected(c.id)
              else setSelected((s) => (s === c.id ? undefined : s))
            }
          }}
          onNodeMouseEnter={(_, n) => setHovered(n.id)}
          onNodeMouseLeave={() => setHovered(undefined)}
          onPaneClick={() => setSelected(undefined)}
          nodesConnectable={false}
          nodesDraggable={false}
          elementsSelectable
          fitView
          fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
          minZoom={0.2}
          maxZoom={1.75}
          proOptions={{ hideAttribution: false }}
        >
          <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} />
          <Panel position="top-left" className="!m-3">
            <ul className="flex max-w-[36rem] flex-wrap gap-x-3 gap-y-1 rounded-lg border bg-card/85 px-3 py-1.5 text-[11px] text-muted-foreground shadow-xs backdrop-blur" aria-label="Legend">
              {kinds.map((k) => (
                <li key={k} className="flex items-center gap-1.5">
                  <span className="size-2 rounded-full" style={{ backgroundColor: KINDS[k].color }} />
                  {KINDS[k].plural}
                </li>
              ))}
            </ul>
          </Panel>
          <Panel position="bottom-left" className="!m-3 flex flex-col rounded-lg border bg-card/90 p-0.5 shadow-xs backdrop-blur">
            <ZoomButton label="Zoom in" onClick={() => void flow.zoomIn({ duration: 200 })}>
              <Plus />
            </ZoomButton>
            <ZoomButton label="Zoom out" onClick={() => void flow.zoomOut({ duration: 200 })}>
              <Minus />
            </ZoomButton>
            <ZoomButton label="Fit the case" onClick={() => void flow.fitView({ padding: 0.12, maxZoom: 1, duration: 300 })}>
              <Maximize />
            </ZoomButton>
          </Panel>
        </ReactFlow>
      </div>
      <aside className="h-[38rem] overflow-y-auto border-t bg-card lg:border-t-0 lg:border-l" data-testid="flow-detail">
        {chosen ? (
          <NodeDetail key={chosen.id} graph={graph} node={chosen} texts={texts} onSelect={select} onOpen={onOpen} onClose={() => setSelected(undefined)} />
        ) : (
          <CaseOverview graph={graph} onSelect={select} />
        )}
      </aside>
    </div>
  )
}

/**
 * A case drawn as a graph from its files' text, with what each part is written as beside it.
 * `onOpen` shows a file, and the lines in it, in the source view.
 */
export function CaseFlow({ texts, onOpen }: { texts: ReadonlyMap<string, string | undefined>; onOpen: (path: string, reveal?: Reveal) => void }) {
  const drawn = useMemo(() => {
    try {
      return caseGraph(texts)
    } catch (err) {
      if (err instanceof YamlProblem) return err
      throw err
    }
  }, [texts])
  if (drawn instanceof YamlProblem)
    return (
      <Alert>
        <FileWarning />
        <AlertTitle>The case cannot be drawn</AlertTitle>
        <AlertDescription className="flex flex-col items-start gap-2">
          <span className="font-mono text-xs">
            {drawn.message} (line {drawn.line})
          </span>
          {texts.has(drawn.path) && (
            <Button size="sm" variant="outline" onClick={() => onOpen(drawn.path, { line: drawn.line })}>
              Open {drawn.path} at line {drawn.line}
            </Button>
          )}
        </AlertDescription>
      </Alert>
    )
  return (
    <ReactFlowProvider>
      <Canvas graph={drawn} texts={texts} onOpen={onOpen} />
    </ReactFlowProvider>
  )
}

// Where each node of a case's graph sits: dagre's layered layout, left to right, so what feeds a
// node (variants, the task) is left of it and what reads it (scorers) right of it.
import dagre from "@dagrejs/dagre"

import type { CaseGraph, CaseNode } from "@/lib/caseGraph"

export const NODE_WIDTH = 236

/** A node's height on the canvas. The card is drawn at exactly this size. */
export function nodeHeight(node: CaseNode): number {
  return node.chips.length ? 92 : 66
}

/** Top-left corners, by node id. */
export function layout(graph: CaseGraph): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: "LR", nodesep: 22, ranksep: 72, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of graph.nodes) g.setNode(n.id, { width: NODE_WIDTH, height: nodeHeight(n) })
  for (const e of graph.edges) g.setEdge(e.from, e.to)
  // Scorers read the run once it ends: keep them in the last column, right of every node that
  // something leads to and that leads nowhere, with edges that only rank and are never drawn.
  // A node with no edges at all stays in the first column.
  const scorers = graph.nodes.filter((n) => n.kind === "scorer")
  const sinks = graph.nodes.filter(
    (n) => n.kind !== "scorer" && graph.edges.some((e) => e.to === n.id) && !graph.edges.some((e) => e.from === n.id)
  )
  for (const sink of sinks)
    for (const scorer of scorers) if (!g.hasEdge(sink.id, scorer.id)) g.setEdge(sink.id, scorer.id, { weight: 0 })
  dagre.layout(g)
  return new Map(
    graph.nodes.map((n) => {
      const { x, y } = g.node(n.id)
      return [n.id, { x: x - NODE_WIDTH / 2, y: y - nodeHeight(n) / 2 }]
    })
  )
}

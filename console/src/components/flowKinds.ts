import {
  Bot,
  Box,
  FileInput,
  KeyRound,
  Layers,
  MessagesSquare,
  Puzzle,
  Shuffle,
  Target,
  type LucideIcon,
} from "lucide-react"

import type { EdgeKind, NodeKind } from "@/lib/caseGraph"

/** How each kind of node is drawn: its icon, its name, and its colour token in index.css. */
export const KINDS: Record<NodeKind, { icon: LucideIcon; label: string; plural: string; color: string }> = {
  variant: { icon: Shuffle, label: "Variant axis", plural: "Variant axes", color: "var(--flow-variant)" },
  task: { icon: FileInput, label: "Task", plural: "Task", color: "var(--flow-task)" },
  agent: { icon: Bot, label: "Agent", plural: "Agents", color: "var(--flow-agent)" },
  channel: { icon: MessagesSquare, label: "Channel", plural: "Channels", color: "var(--flow-channel)" },
  extension: { icon: Puzzle, label: "Extension", plural: "Extensions", color: "var(--flow-extension)" },
  sandbox: { icon: Box, label: "Sandbox", plural: "Sandboxes", color: "var(--flow-sandbox)" },
  profile: { icon: Layers, label: "Sandbox profile", plural: "Profiles", color: "var(--flow-profile)" },
  canary: { icon: KeyRound, label: "Canary", plural: "Canaries", color: "var(--flow-canary)" },
  scorer: { icon: Target, label: "Scorer", plural: "Scorers", color: "var(--flow-scorer)" },
}

/** The order kinds are listed in, roughly left to right on the canvas. */
export const KIND_ORDER: NodeKind[] = ["variant", "task", "agent", "channel", "extension", "sandbox", "profile", "canary", "scorer"]

/** An edge in words, from either end: `out` reads from its source, `in` from its target. */
export const RELATIONS: Record<EdgeKind, { out: string; in: string; dash?: string; arrow: boolean }> = {
  varies: { out: "Varies", in: "Varied by", dash: "5 4", arrow: true },
  input: { out: "First message of", in: "First message from", arrow: true },
  member: { out: "Member of", in: "Members", arrow: false },
  runs_in: { out: "Runs in", in: "Runs", arrow: true },
  profile: { out: "Profile", in: "Used by", arrow: true },
  holds: { out: "Holds", in: "Held in", arrow: true },
  acts_on: { out: "Acts on", in: "Acted on by", dash: "6 3", arrow: true },
  reads: { out: "Read by", in: "Reads", dash: "1.5 4", arrow: true },
}

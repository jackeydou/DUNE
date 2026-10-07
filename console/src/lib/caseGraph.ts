// A case's `case.yaml` and env file read as a graph for the flow view: agents, the channels and
// sandboxes they use, the profiles and canaries behind those, extensions, scorers, and the
// variant axes that change them. A drawing, not a validation: it reads whatever parses and
// leaves checking to the loader, whose verdict is the revision's `loadError`.
import {
  isMap,
  isNode,
  isScalar,
  isSeq,
  LineCounter,
  parseDocument,
  type Document,
} from "yaml"

export type NodeKind =
  | "variant"
  | "task"
  | "agent"
  | "channel"
  | "extension"
  | "sandbox"
  | "profile"
  | "canary"
  | "scorer"

/** Where a node is written: its lines in a YAML file, dedented. */
export interface Source {
  path: string
  /** 1-based line of the block's first line. */
  line: number
  text: string
}

export interface CaseNode {
  /** `<kind>:<name>`, unique in the graph. */
  id: string
  kind: NodeKind
  title: string
  subtitle: string
  chips: string[]
  fields: [string, string][]
  source?: Source
  /** Case files the node points to, after `${variant.x}` is expanded; some may be missing. */
  files: string[]
}

export type EdgeKind =
  | "varies"
  | "input"
  | "member"
  | "runs_in"
  | "profile"
  | "holds"
  | "acts_on"
  | "reads"

export interface CaseEdge {
  from: string
  to: string
  kind: EdgeKind
}

export interface CaseSummary {
  id: string
  workspace: string
  category: string
  description: string
  epochs: number
  turnPolicy: string
  limits: [string, string][]
  /** Variants per choice of models: the product of the axes' lengths. */
  variants: number
  modelSlots: string[]
  environment: string
}

export interface CaseGraph {
  summary: CaseSummary
  nodes: CaseNode[]
  edges: CaseEdge[]
}

/** A file that does not parse as YAML, with the first error's line. */
export class YamlProblem extends Error {
  constructor(
    readonly path: string,
    readonly line: number,
    message: string
  ) {
    super(message)
  }
}

type Obj = Record<string, unknown>

const obj = (v: unknown): Obj =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {}
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])
const str = (v: unknown): string =>
  typeof v === "string" ? v : v === undefined || v === null ? "" : String(v)
const strings = (v: unknown): string[] =>
  list(v).filter((s): s is string => typeof s === "string")
/** Detail rows without the empty ones. */
const rows = (...pairs: [string, string][]): [string, string][] => pairs.filter(([, v]) => v)

/** A value as one line: scalars as written, collections as compact JSON. */
export function inline(v: unknown): string {
  if (v === null || v === undefined) return ""
  if (typeof v === "object") return JSON.stringify(v)
  return String(v)
}

const VARIANT_REF = /\$\{variant\.([a-z][a-z0-9_]*)\}/g

function axesIn(text: string): string[] {
  return [...new Set([...text.matchAll(VARIANT_REF)].map((m) => m[1]))]
}

/** A path with `${variant.x}` in it, as every path it can be. Unknown axes stay as written. */
export function expandPath(path: string, axes: Map<string, unknown[]>): string[] {
  let out = [path]
  for (const axis of axesIn(path)) {
    const values = axes.get(axis)
    if (!values?.length) continue
    out = out.flatMap((p) =>
      values.map((value) => p.replaceAll(`\${variant.${axis}}`, inline(value)))
    )
  }
  return out
}

/** A YAML node's offsets: start, end of its value, end with trailing comments. */
const rangeOf = (n: unknown) => (isNode(n) ? (n.range ?? undefined) : undefined)

class Yaml {
  readonly doc: Document
  readonly value: Obj
  private readonly lines = new LineCounter()

  constructor(
    readonly path: string,
    readonly text: string
  ) {
    this.doc = parseDocument(text, { lineCounter: this.lines, prettyErrors: false })
    const error = this.doc.errors[0]
    if (error)
      throw new YamlProblem(
        path,
        this.lines.linePos(error.pos[0]).line,
        `${path}: ${error.message.split("\n")[0]}`
      )
    this.value = obj(this.doc.toJS())
  }

  /** The lines of the entry at `path`: a sequence item, or a mapping's key with its value. */
  source(path: (string | number)[]): Source | undefined {
    const parent = path.length > 1 ? this.doc.getIn(path.slice(0, -1), true) : this.doc.contents
    const last = path[path.length - 1]
    let range: [number, number] | undefined
    if (isSeq(parent) && typeof last === "number") {
      const item = rangeOf(parent.items[last])
      if (item) range = [item[0], item[1]]
    } else if (isMap(parent)) {
      const pair = parent.items.find((p) => isScalar(p.key) && p.key.value === last)
      const key = rangeOf(pair?.key)
      const end = rangeOf(pair?.value)?.[1] ?? key?.[1]
      if (key && end !== undefined) range = [key[0], end]
    }
    if (!range) return undefined
    const start = this.text.lastIndexOf("\n", range[0] - 1) + 1
    const stop = this.text.indexOf("\n", Math.max(range[1] - 1, start))
    const lines = this.text
      .slice(start, stop === -1 ? undefined : stop)
      .replace(/\s+$/, "")
      .split("\n")
    const indent = Math.min(
      ...lines.filter((l) => l.trim()).map((l) => l.length - l.trimStart().length)
    )
    return {
      path: this.path,
      line: this.lines.linePos(range[0]).line,
      text: lines.map((l) => l.slice(indent)).join("\n"),
    }
  }
}

/** The intervention as a chip: its name, and its config when that fits in a few words. */
function interventionChip(entry: unknown): string {
  if (typeof entry === "string") return entry
  const [name, config] = Object.entries(obj(entry))[0] ?? ["?", {}]
  const terms = Object.entries(obj(config))
    .filter(([k]) => k !== "content" && k !== "prompt")
    .map(([k, v]) => `${k}=${inline(v)}`)
    .join(" ")
  return terms && terms.length <= 24 ? `${name} ${terms}` : name
}

/** `files` under a directory, or the path itself when it is a file of the case. */
function under(dir: string, paths: readonly string[]): string[] {
  const clean = dir.replace(/\/+$/, "")
  if (paths.includes(clean)) return [clean]
  const inside = paths.filter((p) => p.startsWith(`${clean}/`))
  return inside.length ? inside : [clean]
}

/** Every string in a config, with the key it sits under. */
function stringsIn(value: unknown, key = ""): [string, string][] {
  if (typeof value === "string") return [[key, value]]
  if (Array.isArray(value)) return value.flatMap((v) => stringsIn(v, key))
  return Object.entries(obj(value)).flatMap(([k, v]) => stringsIn(v, k))
}

/**
 * The graph of a case, from its files' text (`undefined` for a binary file). Throws
 * `YamlProblem` when `case.yaml` or the env file does not parse; a missing env file draws the
 * case without sandboxes' profiles.
 */
export function caseGraph(texts: ReadonlyMap<string, string | undefined>): CaseGraph {
  const caseText = texts.get("case.yaml")
  if (caseText === undefined)
    throw new YamlProblem("case.yaml", 1, "The case has no case.yaml. Add it in the source view.")
  const caseYaml = new Yaml("case.yaml", caseText)
  const c = caseYaml.value
  const environment = str(c.environment) || "env.yaml"
  const envText = texts.get(environment)
  const envYaml = envText === undefined ? undefined : new Yaml(environment, envText)
  const env = envYaml?.value ?? {}
  const paths = [...texts.keys()]

  const nodes: CaseNode[] = []
  const edges: CaseEdge[] = []
  const has = (id: string) => nodes.some((n) => n.id === id)
  const edge = (from: string, to: string, kind: EdgeKind) => {
    if (from !== to && has(from) && has(to) && !edges.some((e) => e.from === from && e.to === to))
      edges.push({ from, to, kind })
  }

  // Variant axes.
  const axes = new Map<string, unknown[]>(
    Object.entries(obj(c.variants)).map(([axis, values]) => [axis, list(values)])
  )
  for (const [axis, values] of axes)
    nodes.push({
      id: `variant:${axis}`,
      kind: "variant",
      title: axis,
      subtitle: `${values.length} value${values.length === 1 ? "" : "s"}`,
      chips: values.map((v) => (Array.isArray(v) && v.length === 0 ? "[] (off)" : inline(v))),
      fields: values.map((v, i) => [`value ${i + 1}`, inline(v) || "[]"]),
      source: caseYaml.source(["variants", axis]),
      files: [],
    })
  const expand = (path: string) => (path ? expandPath(path, axes) : [])
  // A name the case refers to (a sandbox, a profile, an agent), as every name it can be: a
  // whole `${variant.x}` is each string in the axis's values, list values included.
  const names = (value: string): string[] => {
    const whole = /^\$\{variant\.([a-z][a-z0-9_]*)\}$/.exec(value)
    if (whole && axes.has(whole[1])) return stringsIn(axes.get(whole[1])).map(([, v]) => v)
    return expand(value)
  }

  // The shared task.
  const swarm = obj(c.swarm)
  const agents = list(swarm.agents).map(obj)
  const taskInput = str(obj(c.task).input)
  if (taskInput)
    nodes.push({
      id: "task:input",
      kind: "task",
      title: taskInput,
      subtitle: "first message of each agent without its own task",
      chips: [],
      fields: [["file", taskInput]],
      source: caseYaml.source(["task"]),
      files: expand(taskInput),
    })

  // Agents.
  const agentIds = agents.map((a) => str(a.id))
  agents.forEach((a, i) => {
    const id = str(a.id) || `agent ${i + 1}`
    const slot = str(a.model_slot) || "default"
    const tools = strings(a.tools)
    const sampling = Object.entries(obj(a.sampling)).map(([k, v]) => `${k}=${inline(v)}`)
    const fields: [string, string][] = [
      ["model slot", slot],
      ["prompt", str(a.prompt)],
      ["task", str(a.task) || (taskInput ? `${taskInput} (task.input)` : "")],
      ["tools", tools.join(", ")],
      ["sandbox", str(a.sandbox) ? `${str(a.sandbox)} (shared)` : `${id} (private)`],
      ["sandbox profile", str(a.sandbox_profile)],
      ["os user", str(a.os_user)],
      ["sampling", sampling.join(", ")],
    ]
    nodes.push({
      id: `agent:${id}`,
      kind: "agent",
      title: id,
      subtitle: `model slot ${slot}`,
      chips: tools,
      fields: rows(...fields),
      source: caseYaml.source(["swarm", "agents", i]),
      files: [...expand(str(a.prompt)), ...expand(str(a.task))],
    })
  })

  // Channels.
  list(swarm.channels).forEach((raw, i) => {
    const ch = obj(raw)
    const id = str(ch.id) || `channel ${i + 1}`
    const members = strings(ch.members)
    const interventions = list(ch.interventions)
    nodes.push({
      id: `channel:${id}`,
      kind: "channel",
      title: id,
      subtitle: `${members.length} members`,
      chips: interventions.map(interventionChip),
      fields: rows(
        ["members", members.join(", ")],
        ...interventions.map((e, j): [string, string] => [
          `intervention ${j + 1}`,
          typeof e === "string" ? e : inline(e),
        ])
      ),
      source: caseYaml.source(["swarm", "channels", i]),
      files: [],
    })
  })

  // Sandboxes: the shared instances env declares, and one private sandbox per other agent.
  const profiles = obj(env.sandbox_profiles)
  const shared = obj(env.sandboxes)
  // Sandbox name → the profiles it can have, over the variants.
  const sandboxProfiles = new Map<string, string[]>()
  for (const [name, raw] of Object.entries(shared)) {
    const profile = str(obj(raw).profile)
    sandboxProfiles.set(name, names(profile))
    nodes.push({
      id: `sandbox:${name}`,
      kind: "sandbox",
      title: name,
      subtitle: "shared sandbox",
      chips: [`profile ${profile || "?"}`],
      fields: [
        ["instance", "shared"],
        ["profile", profile],
      ],
      source: envYaml?.source(["sandboxes", name]),
      files: [],
    })
  }
  agents.forEach((a, i) => {
    const id = agentIds[i]
    if (!id || str(a.sandbox)) return
    const profile = str(a.sandbox_profile) || "default"
    sandboxProfiles.set(id, names(profile))
    nodes.push({
      id: `sandbox:${id}`,
      kind: "sandbox",
      title: id,
      subtitle: "private sandbox",
      chips: [`profile ${profile}`],
      fields: [
        ["instance", `private to ${id}`],
        ["profile", profile],
      ],
      files: [],
    })
  })

  for (const [name, raw] of Object.entries(profiles)) {
    const p = obj(raw)
    const mounts = list(p.fs).map(obj)
    const limits = Object.entries(obj(p.limits)).map(([k, v]) => `${k} ${inline(v)}`)
    const copies = list(p.files).map(obj)
    nodes.push({
      id: `profile:${name}`,
      kind: "profile",
      title: name,
      subtitle: str(p.image) || "no image",
      chips: [
        ...mounts.map((m) => `${str(m.path)}${m.protected ? " protected" : ""}`),
        ...limits,
      ],
      fields: rows(
        ["image", str(p.image)],
        ...mounts.map((m): [string, string] => [
          "mount",
          [str(m.path), str(m.mode) || "rw", m.protected ? "protected" : ""]
            .filter(Boolean)
            .join(" · "),
        ]),
        ["limits", limits.join(", ")],
        ...copies.map((f): [string, string] => ["copies", `${str(f.from)} → ${str(f.to)}`])
      ),
      source: envYaml?.source(["sandbox_profiles", name]),
      files: copies.flatMap((f) => under(str(f.from), paths)),
    })
  }
  for (const [sandbox, profiles] of sandboxProfiles)
    for (const profile of profiles) edge(`sandbox:${sandbox}`, `profile:${profile}`, "profile")

  list(env.canaries).forEach((raw, i) => {
    const k = obj(raw)
    const id = str(k.id) || `canary ${i + 1}`
    nodes.push({
      id: `canary:${id}`,
      kind: "canary",
      title: id,
      subtitle: str(k.path),
      chips: [`in ${str(k.sandbox) || "?"}`],
      fields: [
        ["sandbox", str(k.sandbox)],
        ["path", str(k.path)],
      ],
      source: envYaml?.source(["canaries", i]),
      files: [],
    })
    for (const sandbox of names(str(k.sandbox))) edge(`sandbox:${sandbox}`, `canary:${id}`, "holds")
  })

  // Extensions.
  const channelIds = new Set(list(swarm.channels).map((ch) => str(obj(ch).id)))
  // By `as`, derived name, and `use`, for scorers that name the extension they read.
  const extensionNames = new Map<string, string>()
  const extensionIds: string[] = []
  list(c.extensions).forEach((raw, i) => {
    const x = obj(raw)
    const use = str(x.use)
    const name = str(x.as) || use.replace(/^case:/, "").split(/[./]/).filter(Boolean).at(-1) || `extension ${i + 1}`
    const id = `extension:${name}:${i}`
    extensionIds.push(id)
    extensionNames.set(name, id)
    extensionNames.set(use, id)
    const config = obj(x.config)
    nodes.push({
      id,
      kind: "extension",
      title: name,
      subtitle: use,
      chips: Object.keys(config),
      fields: rows(
        ["use", use],
        ["as", str(x.as)],
        ...Object.entries(config).map(([k, v]): [string, string] => [k, inline(v)])
      ),
      source: caseYaml.source(["extensions", i]),
      files: use.startsWith("case:") ? [use.slice("case:".length)] : [],
    })
  })

  // Scorers.
  const sandboxIds = [...sandboxProfiles.keys()]
  const canaryIds = list(env.canaries).map((k) => str(obj(k).id))
  list(c.scorers).forEach((raw, i) => {
    const s = obj(raw)
    const id = str(s.id) || `scorer ${i + 1}`
    const type = str(s.type)
    const skip = new Set(["id", "type", "meaning"])
    nodes.push({
      id: `scorer:${id}`,
      kind: "scorer",
      title: id,
      subtitle: type,
      chips: [],
      fields: rows(
        ["type", type],
        ["meaning", str(s.meaning).trim()],
        ...Object.entries(s)
          .filter(([k]) => !skip.has(k))
          .map(([k, v]): [string, string] => [k, inline(v)])
      ),
      source: caseYaml.source(["scorers", i]),
      files: type === "command" ? expand(str(s.script)) : [],
    })
  })

  // Edges between what is declared above.
  for (const n of nodes) {
    for (const axis of axesIn(n.source?.text ?? "")) edge(`variant:${axis}`, n.id, "varies")
  }
  agents.forEach((a, i) => {
    const id = `agent:${agentIds[i]}`
    if (!str(a.task)) edge("task:input", id, "input")
    for (const sandbox of str(a.sandbox) ? names(str(a.sandbox)) : [agentIds[i]]) edge(id, `sandbox:${sandbox}`, "runs_in")
  })
  list(swarm.channels).forEach((raw) => {
    const ch = obj(raw)
    for (const member of strings(ch.members).flatMap(names)) edge(`agent:${member}`, `channel:${str(ch.id)}`, "member")
  })
  list(c.extensions).forEach((raw, i) => {
    const x = obj(raw)
    const name = extensionIds[i]
    for (const [key, value] of stringsIn(x.config)) {
      for (const t of names(value)) {
        if (key === "sandbox") edge(name, `sandbox:${t}`, "acts_on")
        else if (agentIds.includes(t)) edge(name, `agent:${t}`, "acts_on")
        else if (channelIds.has(t)) edge(name, `channel:${t}`, "acts_on")
      }
    }
  })
  list(c.scorers).forEach((raw) => {
    const s = obj(raw)
    const id = `scorer:${str(s.id)}`
    const fromSandboxes = (names: string[]) => names.forEach((n) => edge(`sandbox:${n}`, id, "reads"))
    switch (str(s.type)) {
      case "command":
        fromSandboxes(names(str(s.sandbox)))
        break
      case "canary":
        if (canaryIds.length) canaryIds.forEach((k) => edge(`canary:${k}`, id, "reads"))
        else fromSandboxes(sandboxIds)
        break
      case "protected_write": {
        const guarded = sandboxIds.filter((sb) =>
          (sandboxProfiles.get(sb) ?? []).some((p) => list(obj(profiles[p]).fs).some((m) => obj(m).protected))
        )
        fromSandboxes(guarded.length ? guarded : sandboxIds)
        break
      }
      case "cross_sandbox":
        fromSandboxes(sandboxIds)
        break
      case "event_value": {
        const from = extensionNames.get(str(s.extension))
        if (from) edge(from, id, "reads")
        else for (const x of extensionNames.values()) edge(x, id, "reads")
        break
      }
    }
    // A scorer that reads no sandbox or extension reads the run's events, which the agents make.
    if (!edges.some((e) => e.to === id && e.kind === "reads"))
      agentIds.forEach((a) => edge(`agent:${a}`, id, "reads"))
  })

  const limits = Object.entries(obj(swarm.limits)).map(([k, v]): [string, string] => [k, inline(v)])
  const variants = [...axes.values()].reduce((n, values) => n * Math.max(values.length, 1), 1)
  return {
    summary: {
      id: str(c.id),
      workspace: str(c.workspace),
      category: str(c.category),
      description: str(c.description).trim(),
      epochs: typeof c.epochs === "number" ? c.epochs : 1,
      turnPolicy: str(swarm.turn_policy) || "round_robin",
      limits,
      variants,
      modelSlots: [...new Set(agents.map((a) => str(a.model_slot) || "default"))],
      environment,
    },
    nodes,
    edges,
  }
}

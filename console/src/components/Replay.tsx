import { ChevronDown } from "lucide-react"
import { useMemo, useState } from "react"

import { ErrorAlert } from "@/components/common"
import { ReplayCausality } from "@/components/ReplayCausality"
import { ReplayInspector } from "@/components/ReplayInspector"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { agentsOf, typesOf } from "@/lib/events"
import {
  describeEvent,
  elapsedLabel,
  replayPhases,
  type ReplayEvent,
} from "@/lib/replay"
import { useRunEvents, type RunEvents } from "@/lib/useRunEvents"
import { cn } from "@/lib/utils"

function TimelineRow({
  item,
  selected,
  onSelect,
}: {
  item: ReplayEvent
  selected: string | undefined
  onSelect: ((id: string) => void) | undefined
}) {
  const { event, title, summary } = item
  const Component = onSelect ? "button" : "div"
  return (
    <Component
      type={onSelect ? "button" : undefined}
      aria-pressed={onSelect ? selected === event.eventId : undefined}
      data-testid="event"
      data-type={event.type}
      className={cn(
        "relative grid w-full grid-cols-[3.9rem_minmax(0,1fr)] items-baseline gap-2 rounded-md px-2 py-1.5 text-left",
        "before:absolute before:top-[1rem] before:left-[3.95rem] before:size-1 before:rounded-full before:bg-muted-foreground/60",
        onSelect &&
          "hover:bg-muted/60 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
        selected === event.eventId && "bg-accent before:bg-primary"
      )}
      onClick={onSelect ? () => onSelect(event.eventId) : undefined}
    >
      <span
        className="truncate font-mono text-[11px] text-muted-foreground tabular-nums"
        title={`Event #${event.seq}`}
      >
        {elapsedLabel(item)}
      </span>
      <span className="min-w-0">
        <span className="flex flex-wrap items-baseline gap-x-2">
          <strong className="text-sm font-medium break-all">{title}</strong>
          <span className="text-xs break-all text-muted-foreground">
            · {event.agentId || "Run"}
          </span>
        </span>
        {summary && (
          <span className="block truncate text-xs text-muted-foreground">
            {summary}
          </span>
        )}
      </span>
    </Component>
  )
}

function Conversation({
  items,
  selected,
  onSelect,
}: {
  items: ReplayEvent[]
  selected: string | undefined
  onSelect: (id: string) => void
}) {
  const chunks: ReplayEvent[][] = []
  for (const item of items) {
    const last = chunks.at(-1)
    if (!last || item.message || last[0].message) chunks.push([item])
    else last.push(item)
  }
  return (
    <div className="flex flex-col gap-3 pb-4">
      {chunks.map((chunk) => {
        const item = chunk[0]
        const { event, message } = item
        return message ? (
          <div
            key={event.eventId}
            className="grid grid-cols-[5rem_minmax(0,1fr)] items-start gap-2"
          >
            <div className="py-1">
              <p className="text-xs font-medium break-all">{message.sender}</p>
              <p className="font-mono text-[11px] text-muted-foreground">
                {elapsedLabel(item)}
              </p>
            </div>
            <button
              type="button"
              data-testid="event"
              data-type={event.type}
              aria-pressed={selected === event.eventId}
              onClick={() => onSelect(event.eventId)}
              className={cn(
                "rounded-md px-2 py-1 text-left text-sm break-words whitespace-pre-wrap hover:bg-muted/60 focus-visible:outline-2 focus-visible:outline-ring",
                selected === event.eventId && "bg-accent"
              )}
            >
              {message.channel && (
                <span className="block text-xs text-muted-foreground">
                  # {message.channel}
                </span>
              )}
              {message.content || "(Empty message)"}
            </button>
          </div>
        ) : (
          <details
            key={event.eventId}
            className="ml-[5.5rem]"
            open={
              chunk.some((item) => item.event.eventId === selected) || undefined
            }
          >
            <summary className="cursor-pointer py-1 text-xs text-muted-foreground hover:text-foreground">
              {chunk.length} background{" "}
              {chunk.length === 1 ? "event" : "events"}
            </summary>
            {chunk.map((entry) => (
              <TimelineRow
                key={entry.event.eventId}
                item={entry}
                selected={selected}
                onSelect={onSelect}
              />
            ))}
          </details>
        )
      })}
    </div>
  )
}

type View = "timeline" | "conversation" | "map"

export function Replay({
  runId,
  stream,
  canFork,
  detail = true,
}: {
  runId: string
  stream: RunEvents
  canFork: boolean
  detail?: boolean
}) {
  const { events, live, error } = stream
  const [view, setView] = useState<View>("timeline")
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [actor, setActor] = useState<string>()
  const [selected, setSelected] = useState<string>()
  const [inspector, setInspector] = useState<"closed" | "wide" | "open">(
    "closed"
  )
  const items = useMemo(() => events.map(describeEvent), [events])
  const byId = useMemo(
    () => new Map(items.map((item) => [item.event.eventId, item])),
    [items]
  )
  const phases = useMemo(() => replayPhases(items), [items])
  const types = useMemo(() => typesOf(events), [events])
  const agents = useMemo(() => agentsOf(events), [events])
  const visible = (item: ReplayEvent) =>
    !hidden.has(item.event.type) &&
    (actor === undefined || actor === item.event.agentId)
  const current = selected ? byId.get(selected) : undefined
  const index = current ? items.indexOf(current) : -1
  const messages = items.some((item) => item.message)
  const select = (id: string) => {
    setSelected(id)
    setInspector("open")
  }
  const exported = canFork && !live

  function switchView(value: string) {
    if (value === "map") {
      if (!current) setSelected(items.at(-1)?.event.eventId)
      setInspector("wide")
    }
    if (value === "timeline" || value === "conversation" || value === "map")
      setView(value)
  }

  return (
    <Tabs
      value={view}
      onValueChange={switchView}
      className="gap-0"
      data-testid="replay"
    >
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1 border-b">
        <TabsList
          variant="line"
          aria-label="Replay view"
          className="gap-5 px-0"
        >
          <TabsTrigger value="timeline" className="px-0">
            Timeline
          </TabsTrigger>
          {detail && (
            <TabsTrigger value="conversation" className="px-0">
              Conversation
            </TabsTrigger>
          )}
          {detail && (
            <TabsTrigger value="map" className="px-0">
              Causal map
            </TabsTrigger>
          )}
        </TabsList>
        <span
          className="pb-1.5 text-xs text-muted-foreground"
          data-testid="event-count"
        >
          {events.length} {events.length === 1 ? "event" : "events"}
          {live && " · Live"}
        </span>
      </div>
      <ErrorAlert title="The event stream stopped" error={error} />
      <div
        className="@container relative flex min-h-80"
        style={{ height: "clamp(24rem, 68dvh, 48rem)" }}
      >
        <div
          className={cn(
            "flex min-w-0 flex-1 flex-col",
            current && inspector === "open" && "invisible @3xl:visible"
          )}
        >
          {view !== "map" && (
            <div className="flex min-h-11 flex-wrap items-center justify-between gap-x-3 py-1 pr-1 @3xl:pr-4">
              <span className="text-xs text-muted-foreground">
                {view === "timeline" ? "Recorded events" : "Channel messages"}
              </span>
              <div className="flex flex-wrap items-center gap-2">
                <NativeSelect
                  size="sm"
                  className="console-quiet-select max-w-36"
                  aria-label="Filter by agent"
                  value={actor === undefined ? "" : `agent:${actor}`}
                  onChange={(e) =>
                    setActor(
                      e.target.value ? e.target.value.slice(6) : undefined
                    )
                  }
                >
                  <NativeSelectOption value="">All agents</NativeSelectOption>
                  {agents.map((agent) => (
                    <NativeSelectOption key={agent} value={`agent:${agent}`}>
                      {agent || "Run"}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="text-xs text-muted-foreground"
                    >
                      Event types{hidden.size > 0 && ` (${hidden.size} hidden)`}{" "}
                      <ChevronDown />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="end"
                    className="max-h-72 overflow-auto"
                  >
                    {types.map((type) => (
                      <DropdownMenuCheckboxItem
                        key={type}
                        checked={!hidden.has(type)}
                        onSelect={(e) => e.preventDefault()}
                        onCheckedChange={(checked) =>
                          setHidden((current) => {
                            const next = new Set(current)
                            if (checked) next.delete(type)
                            else next.add(type)
                            return next
                          })
                        }
                      >
                        {type}
                      </DropdownMenuCheckboxItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
          )}
          <TabsContent
            value="timeline"
            className="min-h-0 overflow-auto pr-1 @3xl:pr-4"
          >
            {items.length === 0 ? (
              <p className="py-4 text-sm text-muted-foreground">
                {live ? "Waiting for events…" : "No events recorded."}
              </p>
            ) : !items.some(visible) ? (
              <p className="py-4 text-sm text-muted-foreground">
                No events match these filters.
              </p>
            ) : (
              phases.map((phase) => {
                const entries = phase.events.filter(visible)
                if (!entries.length) return null
                return (
                  <section key={phase.events[0].event.eventId} className="mb-5">
                    <h3 className="mb-1 pl-[4.9rem] text-sm font-semibold">
                      {phase.title}
                    </h3>
                    <div className="relative before:absolute before:top-4 before:bottom-4 before:left-16 before:w-px before:bg-border">
                      {entries.map((item) => (
                        <TimelineRow
                          key={item.event.eventId}
                          item={item}
                          selected={selected}
                          onSelect={detail ? select : undefined}
                        />
                      ))}
                    </div>
                  </section>
                )
              })
            )}
          </TabsContent>
          <TabsContent
            value="conversation"
            className="min-h-0 overflow-auto pr-1 @3xl:pr-4"
          >
            {!messages ? (
              <p className="py-4 text-sm text-muted-foreground">
                No channel messages recorded.
              </p>
            ) : (
              <Conversation
                items={items.filter(visible)}
                selected={selected}
                onSelect={select}
              />
            )}
          </TabsContent>
          <TabsContent
            value="map"
            className="flex min-h-0 flex-col pt-3 pr-1 @3xl:pr-4"
          >
            {current ? (
              <ReplayCausality
                map
                runId={runId}
                event={current}
                byId={byId}
                firstId={events[0]?.eventId}
                exported={exported}
                onSelect={select}
              />
            ) : (
              <p className="text-sm text-muted-foreground">
                Select a timeline event to follow its parents.
              </p>
            )}
          </TabsContent>
        </div>
        {current && inspector !== "closed" ? (
          <ReplayInspector
            runId={runId}
            event={current}
            byId={byId}
            firstId={events[0]?.eventId}
            canFork={canFork}
            exported={exported}
            previous={items[index - 1]}
            next={items[index + 1]}
            wideOnly={inspector === "wide"}
            onSelect={select}
            onClose={() => {
              setInspector("closed")
              if (view !== "map") setSelected(undefined)
            }}
          />
        ) : (
          detail && (
            <aside className="hidden w-[34%] min-w-64 shrink-0 border-l py-3 pl-5 text-xs text-muted-foreground @3xl:block">
              Select an event to see its details and causes.
            </aside>
          )
        )}
      </div>
    </Tabs>
  )
}

export function StreamedReplay({ runId }: { runId: string }) {
  return (
    <Replay
      runId={runId}
      stream={useRunEvents(runId)}
      canFork={false}
      detail={false}
    />
  )
}

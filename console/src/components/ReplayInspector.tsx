import { fromJson } from "@bufbuild/protobuf"
import { useMutation, useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { ChevronDown, ChevronUp, X } from "lucide-react"
import { useState } from "react"

import { runs } from "@/api"
import { ErrorAlert, Id, Loading, Verbatim } from "@/components/common"
import { ReplayCausality } from "@/components/ReplayCausality"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { ForkEditSchema } from "@/gen/swarmeval/api/v1/run_pb"
import { prettyPayload, type RunEvent } from "@/lib/events"
import { modelsOf } from "@/lib/models"
import { elapsedLabel, type ReplayEvent } from "@/lib/replay"
import { cn } from "@/lib/utils"

/** One select per model slot of the source run: keep its model, or run the slot on another
 * from the fork point. */
function ModelSwaps({
  runId,
  swaps,
  onChange,
}: {
  runId: string
  swaps: Record<string, string>
  onChange: (swaps: Record<string, string>) => void
}) {
  const source = useQuery({
    queryKey: ["run", runId],
    queryFn: async () => (await runs.getRun({ runId })).run,
  })
  const served = useQuery({
    queryKey: ["models"],
    queryFn: () => runs.listModels({}),
  })
  if (source.error ?? served.error)
    return (
      <ErrorAlert
        title="The models did not load"
        error={source.error ?? served.error}
      />
    )
  if (!source.data || !served.data) return <Loading what="the run's models" />
  const current = modelsOf(source.data.variantValues)
  return (
    <>
      {Object.entries(current).map(([slot, model]) => (
        <Field key={slot}>
          <FieldLabel htmlFor={`fork-model-${slot}`}>
            Model for slot <span className="font-mono">{slot}</span>
          </FieldLabel>
          <NativeSelect
            id={`fork-model-${slot}`}
            value={swaps[slot] ?? ""}
            onChange={(e) => onChange({ ...swaps, [slot]: e.target.value })}
          >
            <NativeSelectOption value="">keep {model}</NativeSelectOption>
            {served.data.models
              .filter((m) => m !== model)
              .map((m) => (
                <NativeSelectOption key={m} value={m}>
                  {m}
                </NativeSelectOption>
              ))}
          </NativeSelect>
        </Field>
      ))}
    </>
  )
}

function ForkDialog({
  runId,
  event,
  onClose,
}: {
  runId: string
  event: RunEvent
  onClose: () => void
}) {
  const navigate = useNavigate()
  const [edits, setEdits] = useState("[]")
  const [swaps, setSwaps] = useState<Record<string, string>>({})
  const fork = useMutation({
    mutationFn: async () => {
      const parsed: unknown = JSON.parse(edits)
      if (!Array.isArray(parsed))
        throw new Error("The edits must be a JSON list.")
      const replacements = Object.entries(swaps)
        .filter(([, model]) => model)
        .map(([slot, model]) => ({
          edit: { case: "replaceModel" as const, value: { slot, model } },
        }))
      return runs.forkRun({
        runId,
        atEventId: event.eventId,
        edits: [
          ...parsed.map((edit) => fromJson(ForkEditSchema, edit)),
          ...replacements,
        ],
      })
    },
    onSuccess: (res) => {
      if (res.run)
        void navigate({ to: "/runs/$runId", params: { runId: res.run.runId } })
    },
  })
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Fork from event #{String(event.seq)}</DialogTitle>
          <DialogDescription>
            A new run goes on from this run's state at the start of the turn
            this event happened in, with the edits applied.
          </DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel htmlFor="fork-edits">Edits</FieldLabel>
          <Textarea
            id="fork-edits"
            className="font-mono text-xs"
            rows={8}
            value={edits}
            onChange={(e) => setEdits(e.target.value)}
          />
          <FieldDescription>
            A JSON list. Each item is one of{" "}
            {`{"replaceMessage": {"agentId", "index", "content"}}`},{" "}
            {`{"deleteMessage": {"agentId", "index"}}`},{" "}
            {`{"replaceDelivery": {"sendEventId", "recipient", "content"}}`}. An
            empty list reruns from here unchanged.
          </FieldDescription>
        </Field>
        <ModelSwaps runId={runId} swaps={swaps} onChange={setSwaps} />
        <ErrorAlert title="The run was not forked" error={fork.error} />
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => fork.mutate()} disabled={fork.isPending}>
            Fork
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ReplayInspector({
  runId,
  event,
  canFork,
  exported,
  byId,
  firstId,
  previous,
  next,
  wideOnly,
  onSelect,
  onClose,
}: {
  runId: string
  event: ReplayEvent
  canFork: boolean
  exported: boolean
  byId: ReadonlyMap<string, ReplayEvent>
  firstId: string | undefined
  previous: ReplayEvent | undefined
  next: ReplayEvent | undefined
  wideOnly: boolean
  onSelect: (id: string) => void
  onClose: () => void
}) {
  const [forking, setForking] = useState(false)
  const raw = event.event
  return (
    <aside
      aria-label="Event inspector"
      className={cn(
        "absolute inset-0 z-10 flex min-w-0 flex-col bg-background px-3 @3xl:static @3xl:flex @3xl:w-[34%] @3xl:min-w-64 @3xl:shrink-0 @3xl:border-l @3xl:pr-0 @3xl:pl-5",
        wideOnly && "hidden"
      )}
      data-testid="event-detail"
    >
      <div className="flex min-h-11 shrink-0 items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          Event inspector · #{String(raw.seq)}
        </span>
        <div className="flex items-center gap-0.5 text-muted-foreground">
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label="Previous event"
            disabled={!previous}
            onClick={() => previous && onSelect(previous.event.eventId)}
          >
            <ChevronUp />
          </Button>
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label="Next event"
            disabled={!next}
            onClick={() => next && onSelect(next.event.eventId)}
          >
            <ChevronDown />
          </Button>
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label="Close"
            onClick={onClose}
          >
            <X />
          </Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto pr-1 pb-4">
        <h3 className="font-heading text-2xl leading-tight break-words">
          {event.title}
        </h3>
        <p className="mt-1 font-mono text-xs break-words text-muted-foreground">
          {raw.type}
        </p>
        <Tabs defaultValue="summary" className="mt-3">
          <TabsList
            variant="line"
            aria-label="Event detail"
            className="w-full justify-start gap-4 border-b px-0"
          >
            <TabsTrigger value="summary" className="px-0 text-xs">
              Summary
            </TabsTrigger>
            <TabsTrigger value="payload" className="px-0 text-xs">
              Payload
            </TabsTrigger>
            <TabsTrigger value="causes" className="px-0 text-xs">
              Causes
            </TabsTrigger>
          </TabsList>
          <TabsContent value="summary" className="space-y-3 pt-1">
            {event.summary && (
              <p className="text-sm break-words whitespace-pre-wrap">
                {event.summary}
              </p>
            )}
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
              <dt className="text-muted-foreground">Agent</dt>
              <dd className="break-words">{raw.agentId || "Run"}</dd>
              {event.elapsed !== undefined && (
                <>
                  <dt className="text-muted-foreground">Elapsed</dt>
                  <dd className="font-mono">{elapsedLabel(event)}</dd>
                </>
              )}
              <dt className="text-muted-foreground">Event ID</dt>
              <dd className="min-w-0">
                <Id className="max-w-full" value={raw.eventId} />
              </dd>
              {event.parentId && (
                <>
                  <dt className="text-muted-foreground">Parent</dt>
                  <dd className="min-w-0">
                    <EventReference
                      id={event.parentId}
                      byId={byId}
                      onSelect={onSelect}
                    />
                  </dd>
                </>
              )}
              {event.evidenceIds.length > 0 && (
                <>
                  <dt className="text-muted-foreground">Evidence</dt>
                  <dd className="flex min-w-0 flex-col items-start gap-1">
                    {event.evidenceIds.map((id) => (
                      <EventReference
                        key={id}
                        id={id}
                        byId={byId}
                        onSelect={onSelect}
                      />
                    ))}
                  </dd>
                </>
              )}
            </dl>
          </TabsContent>
          <TabsContent value="payload" className="space-y-2 pt-1">
            <h4 className="text-xs font-medium text-muted-foreground">
              Stored event
            </h4>
            <Verbatim className="rounded-md border-0">
              {prettyPayload(raw)}
            </Verbatim>
          </TabsContent>
          <TabsContent value="causes" className="pt-1">
            <ReplayCausality
              runId={runId}
              event={event}
              byId={byId}
              firstId={firstId}
              exported={exported}
              onSelect={onSelect}
            />
          </TabsContent>
        </Tabs>
        {canFork && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="mt-4 -ml-2 text-primary"
            onClick={() => setForking(true)}
          >
            Fork from here
          </Button>
        )}
      </div>
      {forking && (
        <ForkDialog
          key={raw.eventId}
          runId={runId}
          event={raw}
          onClose={() => setForking(false)}
        />
      )}
    </aside>
  )
}

function EventReference({
  id,
  byId,
  onSelect,
}: {
  id: string
  byId: ReadonlyMap<string, ReplayEvent>
  onSelect: (id: string) => void
}) {
  const item = byId.get(id)
  return item ? (
    <button
      type="button"
      className="text-left break-words text-primary hover:underline"
      onClick={() => onSelect(id)}
    >
      #{String(item.event.seq)} {item.title}
    </button>
  ) : (
    <span className="font-mono break-all text-muted-foreground">{id}</span>
  )
}

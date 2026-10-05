import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { errorText, runs } from "@/api"
import type { RunEvent } from "@/lib/events"
import { endSessionOn } from "@/session"

export interface RunEvents {
  events: RunEvent[]
  /** The stream is open: the run may still add events. */
  live: boolean
  error: string | undefined
}

/** A run's events from the first, appended as they happen until the run finishes. */
export function useRunEvents(runId: string): RunEvents {
  const client = useQueryClient()
  const [state, setState] = useState<RunEvents>({ events: [], live: true, error: undefined })
  useEffect(() => {
    const abort = new AbortController()
    setState({ events: [], live: true, error: undefined })
    void (async () => {
      try {
        let pending: RunEvent[] = []
        let scheduled = false
        const flush = () => {
          scheduled = false
          const batch = pending
          pending = []
          setState((s) => ({ ...s, events: [...s.events, ...batch] }))
        }
        for await (const event of runs.streamEvents({ runId }, { signal: abort.signal })) {
          pending.push(event)
          // One render per frame, however fast events arrive.
          if (!scheduled) {
            scheduled = true
            requestAnimationFrame(flush)
          }
        }
        if (pending.length) flush()
        setState((s) => ({ ...s, live: false }))
      } catch (err) {
        if (abort.signal.aborted) return
        // The stream is not a query, so it reports an ended session itself.
        endSessionOn(client, err)
        setState((s) => ({ ...s, live: false, error: errorText(err) }))
      }
    })()
    return () => abort.abort()
  }, [runId, client])
  return state
}

// Times as tables show them: how long ago, and how long something took.
import { timestampDate, type Timestamp } from "@bufbuild/protobuf/wkt"
import { formatDistanceStrict, intervalToDuration } from "date-fns"

/** "3 minutes ago"; "—" for a time that has not happened. `now` is for tests. */
export function ago(ts: Timestamp | undefined, now: Date = new Date()): string {
  return ts
    ? formatDistanceStrict(timestampDate(ts), now, { addSuffix: true })
    : "—"
}

/** "1h 4m", "2m 31s", "12s": the two largest units. A start with no end runs until `now`;
 * no start is "—". */
export function duration(
  start: Timestamp | undefined,
  end: Timestamp | undefined,
  now: Date = new Date()
): string {
  if (!start) return "—"
  const from = timestampDate(start)
  const to = end ? timestampDate(end) : now
  if (to.getTime() - from.getTime() < 1000) return "<1s"
  const d = intervalToDuration({ start: from, end: to })
  const parts: [number | undefined, string][] = [
    [d.years, "y"],
    [d.months, "mo"],
    [d.days, "d"],
    [d.hours, "h"],
    [d.minutes, "m"],
    [d.seconds, "s"],
  ]
  return parts
    .filter(([n]) => n)
    .slice(0, 2)
    .map(([n, unit]) => `${n}${unit}`)
    .join(" ")
}

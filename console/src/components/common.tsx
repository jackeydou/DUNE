import type { JsonValue } from "@bufbuild/protobuf"
import { Check, Copy } from "lucide-react"
import { Fragment, useState, type ReactNode } from "react"

import { errorText } from "@/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

/** A page: the bar with the sidebar toggle and where the reader is, then the page's own title,
 * one line saying what it is, its actions, and its content. `crumbs` are links to the pages
 * above this one; `label` names this one there when `title` is not plain text. */
export function Page({
  title,
  label,
  description,
  crumbs = [],
  actions,
  children,
}: {
  title: ReactNode
  label?: string
  description?: ReactNode
  crumbs?: ReactNode[]
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <>
      <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/90 px-4 backdrop-blur">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mr-1 data-[orientation=vertical]:h-4"
        />
        <Breadcrumb>
          <BreadcrumbList>
            {crumbs.map((crumb, i) => (
              <Fragment key={i}>
                <BreadcrumbItem>
                  <BreadcrumbLink asChild>{crumb}</BreadcrumbLink>
                </BreadcrumbItem>
                <BreadcrumbSeparator />
              </Fragment>
            ))}
            <BreadcrumbItem>
              <BreadcrumbPage className="max-w-[40ch] truncate">
                {label ?? title}
              </BreadcrumbPage>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>
      </header>
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <h1 className="font-heading text-2xl font-medium tracking-tight">
              {title}
            </h1>
            {description && (
              <div className="text-sm text-muted-foreground">{description}</div>
            )}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
        {children}
      </div>
    </>
  )
}

/** A titled block of a page, on a card. */
export function Section({
  title,
  description,
  actions,
  children,
  className,
  ...rest
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  "data-testid"?: string
}) {
  return (
    <section
      className={cn(
        "flex flex-col rounded-xl border bg-card shadow-xs",
        className
      )}
      {...rest}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          {description && (
            <div className="text-xs text-muted-foreground">{description}</div>
          )}
        </div>
        {actions && (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        )}
      </div>
      <div className="flex flex-col gap-3 p-4">{children}</div>
    </section>
  )
}

/** Name and value pairs, in as many columns as fit. */
export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-x-6 gap-y-4">
      {items.map(([name, value]) => (
        <div key={name} className="flex min-w-0 flex-col gap-1">
          <dt className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {name}
          </dt>
          <dd className="min-w-0 text-sm break-words">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** An alert's classes for something that went wrong. The theme's own destructive is near-black. */
export const DANGER_ALERT = "border-danger/30 bg-danger/5 text-danger"

/** A failed call, with edge's message as it came. */
export function ErrorAlert({
  title,
  error,
}: {
  title: string
  error: unknown
}) {
  if (!error) return null
  return (
    <Alert role="alert" className={DANGER_ALERT}>
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="whitespace-pre-wrap text-danger/90">
        {typeof error === "string" ? error : errorText(error)}
      </AlertDescription>
    </Alert>
  )
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Spinner /> Loading {what}…
    </div>
  )
}

// Static class names, so Tailwind finds them.
const STATUS_TONE: Record<string, string> = {
  done: "bg-success/10 text-success [&>i]:bg-success",
  running: "bg-info/10 text-info [&>i]:bg-info [&>i]:animate-pulse",
  queued: "bg-muted text-muted-foreground [&>i]:bg-muted-foreground/60",
  paused: "bg-warning/15 text-warning [&>i]:bg-warning",
  failed: "bg-danger/10 text-danger [&>i]:bg-danger",
  interrupted: "bg-danger/10 text-danger [&>i]:bg-danger",
  cancelled: "bg-muted text-muted-foreground [&>i]:bg-muted-foreground/60",
}

/** A run's or a job's state, as a coloured pill. */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      data-status={status}
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-1.5 rounded-full px-2 font-sans text-xs font-medium whitespace-nowrap",
        STATUS_TONE[status] ?? STATUS_TONE.queued
      )}
    >
      <i className="size-1.5 rounded-full" aria-hidden />
      {status}
    </span>
  )
}

/** How many runs are in each state: a pill per state, with a count past one. */
export function StatusCounts({ statuses }: { statuses: string[] }) {
  const counts = new Map<string, number>()
  for (const s of statuses) counts.set(s, (counts.get(s) ?? 0) + 1)
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {[...counts].map(([status, n]) => (
        <span
          key={status}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground"
        >
          <StatusBadge status={status} />
          {n > 1 && <span className="tabular-nums">×{n}</span>}
        </span>
      ))}
    </span>
  )
}

function scalar(value: JsonValue): string {
  return typeof value === "string" ? value : JSON.stringify(value)
}

/** A variant's axis values as `axis | value` chips; "—" for none. */
export function VariantChips({
  values,
}: {
  values: { [axis: string]: JsonValue } | undefined
}) {
  const entries = Object.entries(values ?? {})
  if (entries.length === 0)
    return <span className="text-muted-foreground">—</span>
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([axis, value]) => (
        <span
          key={axis}
          className="inline-flex max-w-full items-center overflow-hidden rounded-md border bg-background font-mono text-[11px] leading-5"
        >
          <span className="border-r bg-muted px-1.5 text-muted-foreground">
            {axis}
          </span>
          <span className="truncate px-1.5">{scalar(value)}</span>
        </span>
      ))}
    </span>
  )
}

/** An identifier in monospace, with a button that copies it. */
export function Id({
  value,
  className,
}: {
  value: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  return (
    <span
      className={cn(
        "inline-flex min-w-0 items-center gap-1 font-mono",
        className
      )}
    >
      <span className="truncate">{value}</span>
      <Button
        variant="ghost"
        size="icon-xs"
        className="text-muted-foreground"
        aria-label={`Copy ${value}`}
        onClick={async () => {
          await navigator.clipboard.writeText(value)
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        }}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </span>
  )
}

/** Text from a run or a case, shown as text: never as HTML or Markdown. */
export function Verbatim({
  children,
  className,
}: {
  children: string
  className?: string
}) {
  return (
    <pre
      className={cn(
        "overflow-auto rounded-lg border bg-muted/60 p-3 font-mono text-xs break-words whitespace-pre-wrap",
        className
      )}
    >
      {children}
    </pre>
  )
}

/** A table on a card, edge to edge. */
export function TableCard({
  children,
  className,
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border bg-card shadow-xs [&_td:first-child]:pl-4 [&_td:last-child]:pr-4 [&_th:first-child]:pl-4 [&_th:last-child]:pr-4 [&_thead]:bg-muted/40",
        className
      )}
    >
      {children}
    </div>
  )
}

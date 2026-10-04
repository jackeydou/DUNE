import type { ReactNode } from "react"

import { errorText } from "@/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Spinner } from "@/components/ui/spinner"

export function Page({ title, actions, children }: { title: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="font-heading text-xl font-semibold">{title}</h1>
        <div className="flex items-center gap-2">{actions}</div>
      </div>
      {children}
    </div>
  )
}

/** A failed call, with edge's message as it came. */
export function ErrorAlert({ title, error }: { title: string; error: unknown }) {
  if (!error) return null
  return (
    <Alert variant="destructive" role="alert">
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="whitespace-pre-wrap">
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

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  done: "default",
  running: "secondary",
  queued: "outline",
  paused: "secondary",
  failed: "destructive",
  interrupted: "destructive",
  cancelled: "outline",
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "outline"} data-status={status}>
      {status}
    </Badge>
  )
}

/** Text from a run or a case, shown as text: never as HTML or Markdown. */
export function Verbatim({ children, className }: { children: string; className?: string }) {
  return (
    <pre className={`overflow-auto rounded-lg bg-muted p-3 font-mono text-xs whitespace-pre-wrap break-words ${className ?? ""}`}>
      {children}
    </pre>
  )
}

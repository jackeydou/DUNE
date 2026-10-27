import { useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Hexagon, LogOut } from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"

import { ErrorAlert, Facts } from "@/components/common"
import { Button } from "@/components/ui/button"
import type { Submitter } from "@/gen/swarmeval/intake/v1/auth_pb"
import type { GetSiteConfigResponse } from "@/gen/swarmeval/intake/v1/site_pb"
import type { Problem } from "@/gen/swarmeval/intake/v1/submission_pb"
import { ReviewState } from "@/gen/swarmeval/intake/v1/submission_pb"
import { auth } from "@/intake/api"
import { groupProblems, stateName } from "@/intake/lib"
import { cn } from "@/lib/utils"

/** The bar every signed-in page sits under: the site, its two pages, and who is signed in. */
export function TopBar({ submitter }: { submitter: Submitter }) {
  const client = useQueryClient()
  return (
    <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-4 border-b bg-background/90 px-4 backdrop-blur">
      <Link to="/" className="flex items-center gap-2 font-heading text-lg">
        <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Hexagon className="size-4" />
        </span>
        SwarmEval case intake
      </Link>
      <nav className="flex items-center gap-3 text-sm">
        <Link to="/" className="text-muted-foreground hover:text-foreground [&.active]:text-foreground" activeOptions={{ exact: true }}>
          My submissions
        </Link>
        <Link to="/new" className="text-muted-foreground hover:text-foreground [&.active]:text-foreground">
          New submission
        </Link>
      </nav>
      <div className="ml-auto flex items-center gap-2 text-sm">
        <span className="font-mono text-muted-foreground">{submitter.githubLogin}</span>
        <Button
          variant="ghost"
          size="sm"
          onClick={async () => {
            await auth.logout({})
            client.clear()
            // A reload, so nothing the reader saw stays in memory.
            window.location.assign("/")
          }}
        >
          <LogOut /> Sign out
        </Button>
      </div>
    </header>
  )
}

/** A page's title, one line about it, its actions, and its content. */
export function IntakePage({
  title,
  description,
  actions,
  children,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="font-heading text-2xl font-medium tracking-tight">{title}</h1>
          {description && <div className="text-sm text-muted-foreground">{description}</div>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  )
}

// Static class names, so Tailwind finds them.
const STATE_TONE: Record<string, string> = {
  submitted: "bg-info/10 text-info [&>i]:bg-info",
  triaged: "bg-warning/15 text-warning [&>i]:bg-warning",
  accepted: "bg-success/10 text-success [&>i]:bg-success",
  rejected: "bg-danger/10 text-danger [&>i]:bg-danger",
  withdrawn: "bg-muted text-muted-foreground [&>i]:bg-muted-foreground/60",
  unknown: "bg-muted text-muted-foreground [&>i]:bg-muted-foreground/60",
}

/** A submission's review state, as a coloured pill. */
export function StateBadge({ state }: { state: ReviewState }) {
  const name = stateName(state)
  return (
    <span
      data-state={name}
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-1.5 rounded-full px-2 font-sans text-xs font-medium whitespace-nowrap",
        STATE_TONE[name]
      )}
    >
      <i className="size-1.5 rounded-full" aria-hidden />
      {name}
    </span>
  )
}

const kib = (bytes: number) => `${Math.round(bytes / 1024)} KiB`

/** What a submission must keep to, as the site's config states it. */
export function Rules({ config }: { config: GetSiteConfigResponse }) {
  return (
    <div className="flex flex-col gap-4 text-sm">
      <Facts
        items={[
          ["Workspace", <span className="font-mono">{config.workspace}</span>],
          ["Images", config.allowedImages.length ? <span className="font-mono">{config.allowedImages.join(", ")}</span> : "none: agents run without a sandbox"],
          ["Size", `${config.maxFiles} files, ${kib(config.maxFileBytes)} each, ${kib(config.maxTotalBytes)} in all; text only`],
          ["License", `${config.license}, terms ${config.termsVersion}`],
        ]}
      />
      <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
        <li>
          A case is a directory in the SwarmEval case format: <span className="font-mono">case.yaml</span>, an optional env file,
          prompts, and a <span className="font-mono">README.md</span>.
        </li>
        <li>
          No case code: no extension may be a <span className="font-mono">case:</span> file. Use the built-in extensions only.
        </li>
        <li>
          No <span className="font-mono">web_request</span>: no agent may reach the internet.
        </li>
        <li>No real credentials, personal data, or real third-party hosts. Payloads target the case's own mock services only.</li>
        <li>Results that single out one model are shared with its vendor 30 days before they are published.</li>
      </ul>
    </div>
  )
}

/** What a check found, by file. */
export function Problems({ problems }: { problems: readonly Problem[] }) {
  return (
    <ul className="flex flex-col gap-3 text-sm" data-testid="problems">
      {groupProblems(problems).map(([path, messages]) => (
        <li key={path} className="flex flex-col gap-1">
          <span className="font-mono text-xs font-medium">{path || "The submission"}</span>
          <ul className="list-disc pl-5 text-danger">
            {messages.map((m, i) => (
              <li key={i} className="whitespace-pre-wrap">
                {m}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  )
}

interface TurnstileApi {
  render(el: HTMLElement, options: Record<string, unknown>): string
  reset(widgetId: string): void
  remove(widgetId: string): void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

const SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
let loading: Promise<TurnstileApi> | undefined

function loadTurnstile(): Promise<TurnstileApi> {
  loading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script")
    script.src = SCRIPT
    script.async = true
    script.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error("Turnstile loaded without its API")))
    script.onerror = () => {
      loading = undefined
      reject(new Error("Cloudflare's Turnstile did not load. Check the connection, then reload the page."))
    }
    document.head.appendChild(script)
  })
  return loading
}

/**
 * Cloudflare's Turnstile widget. `onToken` gets the token for one save, and `undefined` when it
 * expires or fails. A token is good for one save only: change `resetKey` after each to get a new
 * one.
 */
export function Turnstile({ siteKey, resetKey, onToken }: { siteKey: string; resetKey: number; onToken: (token: string | undefined) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const widget = useRef<{ api: TurnstileApi; id: string }>(undefined)
  const report = useRef(onToken)
  const [error, setError] = useState<unknown>()
  useEffect(() => {
    report.current = onToken
  })
  useEffect(() => {
    let gone = false
    loadTurnstile().then(
      (api) => {
        if (gone || !box.current) return
        const id = api.render(box.current, {
          sitekey: siteKey,
          callback: (token: string) => report.current(token),
          "expired-callback": () => report.current(undefined),
          "error-callback": () => report.current(undefined),
        })
        widget.current = { api, id }
      },
      (err: unknown) => setError(err)
    )
    return () => {
      gone = true
      if (widget.current) widget.current.api.remove(widget.current.id)
      widget.current = undefined
    }
  }, [siteKey])
  useEffect(() => {
    if (resetKey === 0 || !widget.current) return
    report.current(undefined)
    widget.current.api.reset(widget.current.id)
  }, [resetKey])
  return (
    <>
      <div ref={box} />
      <ErrorAlert title="The bot check did not load" error={error} />
    </>
  )
}

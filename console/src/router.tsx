import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRootRoute, createRoute, createRouter, Link, Navigate, Outlet } from "@tanstack/react-router"

import { auth } from "@/api"
import { Loading } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Toaster } from "@/components/ui/sonner"
import { Account } from "@/pages/Account"
import { Analysis } from "@/pages/Analysis"
import { CaseDetail } from "@/pages/CaseDetail"
import { Cases } from "@/pages/Cases"
import { Login } from "@/pages/Login"
import { NewCase } from "@/pages/NewCase"
import { Compare, RunDetail } from "@/pages/RunDetail"
import { Runs, type RunsSearch } from "@/pages/Runs"
import { Users } from "@/pages/Users"
import { isAdmin, useSession } from "@/session"

const queries = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
})

function Root() {
  return (
    <QueryClientProvider client={queries}>
      <Outlet />
      <Toaster />
    </QueryClientProvider>
  )
}

/** Every page but sign-in: the navigation, and the page only for a signed-in user. */
function Shell() {
  const { user } = useSession()
  if (user === undefined) return <div className="p-6"><Loading what="your session" /></div>
  if (user === null) return <Login />
  const link = "rounded-md px-2 py-1 text-sm text-muted-foreground hover:text-foreground [&.active]:bg-muted [&.active]:text-foreground"
  return (
    <div className="flex min-h-svh flex-col">
      <header className="flex items-center justify-between gap-4 border-b px-6 py-2">
        <nav className="flex items-center gap-1">
          <span className="mr-4 font-heading font-semibold">SwarmEval</span>
          <Link to="/runs" className={link}>Runs</Link>
          <Link to="/cases" className={link}>Cases</Link>
          <Link to="/analysis" className={link}>Analysis</Link>
          {isAdmin(user) && <Link to="/users" className={link}>Users</Link>}
        </nav>
        <div className="flex items-center gap-2 text-sm">
          <Link to="/account" className={link} data-testid="whoami">{user.username}</Link>
          <Button
            variant="ghost"
            size="sm"
            onClick={async () => {
              await auth.logout({})
              // A full load: nothing the signed-out user read stays in memory.
              window.location.assign("/")
            }}
          >
            Sign out
          </Button>
        </div>
      </header>
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}

const text = (value: unknown): string | undefined => (typeof value === "string" && value ? value : undefined)

const root = createRootRoute({ component: Root })
const app = createRoute({ getParentRoute: () => root, id: "app", component: Shell })
const index = createRoute({ getParentRoute: () => app, path: "/", component: () => <Navigate to="/runs" /> })
const runsRoute = createRoute({
  getParentRoute: () => app,
  path: "/runs",
  component: Runs,
  validateSearch: (s: Record<string, unknown>): RunsSearch => ({
    workspace: text(s.workspace),
    case: text(s.case),
    suite: text(s.suite),
    status: text(s.status),
    submission: text(s.submission),
  }),
})
const runRoute = createRoute({ getParentRoute: () => app, path: "/runs/$runId", component: RunDetail })
const compareRoute = createRoute({
  getParentRoute: () => app,
  path: "/compare",
  component: Compare,
  validateSearch: (s: Record<string, unknown>) => ({ a: text(s.a) ?? "", b: text(s.b) ?? "" }),
})
const casesRoute = createRoute({ getParentRoute: () => app, path: "/cases", component: Cases })
const newCaseRoute = createRoute({ getParentRoute: () => app, path: "/cases/new", component: NewCase })
const caseRoute = createRoute({ getParentRoute: () => app, path: "/cases/$workspace/$caseId", component: CaseDetail })
const analysisRoute = createRoute({ getParentRoute: () => app, path: "/analysis", component: Analysis })
const accountRoute = createRoute({ getParentRoute: () => app, path: "/account", component: Account })
const usersRoute = createRoute({ getParentRoute: () => app, path: "/users", component: Users })

export const router = createRouter({
  routeTree: root.addChildren([
    app.addChildren([index, runsRoute, runRoute, compareRoute, casesRoute, newCaseRoute, caseRoute, analysisRoute, accountRoute, usersRoute]),
  ]),
})

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}

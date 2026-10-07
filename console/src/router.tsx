import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRootRoute, createRoute, createRouter, Navigate, Outlet } from "@tanstack/react-router"

import { Code, isCode } from "@/api"
import { AppSidebar } from "@/components/AppSidebar"
import { Loading } from "@/components/common"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Account } from "@/pages/Account"
import { Analysis } from "@/pages/Analysis"
import { CaseDetail, type CaseSearch } from "@/pages/CaseDetail"
import { Cases } from "@/pages/Cases"
import { Login } from "@/pages/Login"
import { NewCase } from "@/pages/NewCase"
import { Compare, RunDetail } from "@/pages/RunDetail"
import { Runs, type RunsSearch } from "@/pages/Runs"
import { Users } from "@/pages/Users"
import { endSessionOn, useSession } from "@/session"

// Any call that finds the session gone ends it in the app too, whichever page made the call.
const queries: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError: (err) => endSessionOn(queries, err) }),
  mutationCache: new MutationCache({ onError: (err) => endSessionOn(queries, err) }),
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: (failures, err) => failures < 1 && !isCode(err, Code.Unauthenticated) },
  },
})

function Root() {
  return (
    <QueryClientProvider client={queries}>
      <Outlet />
      <Toaster />
    </QueryClientProvider>
  )
}

/** Every page but sign-in: the sidebar, and the page only for a signed-in user. */
function Shell() {
  const { user } = useSession()
  if (user === undefined) return <div className="p-6"><Loading what="your session" /></div>
  if (user === null) return <Login />
  return (
    <TooltipProvider delayDuration={300}>
      <SidebarProvider>
        <AppSidebar user={user} />
        <SidebarInset className="min-w-0">
          <Outlet />
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
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
const caseRoute = createRoute({
  getParentRoute: () => app,
  path: "/cases/$workspace/$caseId",
  component: CaseDetail,
  validateSearch: (s: Record<string, unknown>): CaseSearch => ({ view: s.view === "source" ? "source" : undefined }),
})
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

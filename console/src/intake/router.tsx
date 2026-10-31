import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router"

import { Loading } from "@/components/common"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { TopBar } from "@/intake/components"
import { NewSubmission } from "@/intake/pages/NewSubmission"
import { SignIn } from "@/intake/pages/SignIn"
import { SubmissionDetail } from "@/intake/pages/SubmissionDetail"
import { Submissions } from "@/intake/pages/Submissions"
import { endSessionOn, useSubmitter } from "@/intake/session"
import { Code, isCode } from "@/lib/errors"

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

/** Every page but sign-in: the bar, and the page only for a signed-in submitter. */
function Shell() {
  const submitter = useSubmitter()
  if (submitter === undefined)
    return (
      <div className="p-6">
        <Loading what="your session" />
      </div>
    )
  if (submitter === null) return <SignIn />
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex min-h-svh flex-col">
        <TopBar submitter={submitter} />
        <Outlet />
      </div>
    </TooltipProvider>
  )
}

const root = createRootRoute({ component: Root })
const app = createRoute({ getParentRoute: () => root, id: "app", component: Shell })
const index = createRoute({ getParentRoute: () => app, path: "/", component: Submissions })
const newRoute = createRoute({ getParentRoute: () => app, path: "/new", component: NewSubmission })
const submissionRoute = createRoute({ getParentRoute: () => app, path: "/submissions/$submissionId", component: SubmissionDetail })

export const router = createRouter({
  routeTree: root.addChildren([app.addChildren([index, newRoute, submissionRoute])]),
})

// The intake pages are their own TypeScript project (tsconfig.intake.json), so this registers
// their router, not the console's.
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}

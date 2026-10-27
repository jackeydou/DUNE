import { Hexagon, LogIn } from "lucide-react"

import { ErrorAlert, Loading } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { SIGN_IN_PATH } from "@/intake/api"
import { Rules } from "@/intake/components"
import { useSiteConfig } from "@/intake/session"

/** What the site is, the rules a case keeps to, and the way in. */
export function SignIn() {
  const config = useSiteConfig()
  return (
    <div className="flex min-h-svh flex-col items-center gap-6 bg-sidebar p-6 pt-16">
      <div className="flex items-center gap-3">
        <div className="flex size-10 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <Hexagon className="size-5" />
        </div>
        <span className="font-heading text-3xl">SwarmEval case intake</span>
      </div>
      <Card className="w-full max-w-2xl shadow-md">
        <CardHeader>
          <CardTitle className="text-lg">Submit a safety or compliance case</CardTitle>
          <CardDescription className="flex flex-col gap-2">
            <span>
              This is a public call for multi-agent evaluation cases: situations where agents acting for different users may
              break a rule, leak what they should keep, or collude. The site only collects cases. Nothing runs here.
            </span>
            <span>
              The maintainers pull submissions, check and review them, and run the accepted ones in periodic evaluation
              rounds. Each round's aggregate results are published, with credit to the authors of the cases it used.
            </span>
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          {config.error ? (
            <ErrorAlert title="The site's rules did not load" error={config.error} />
          ) : !config.data ? (
            <Loading what="the rules" />
          ) : (
            <Rules config={config.data} />
          )}
          <Button asChild className="self-start">
            {/* A navigation, not a call: GitHub's sign-in page takes over, then sends the reader back. */}
            <a href={SIGN_IN_PATH}>
              <LogIn /> Sign in with GitHub
            </a>
          </Button>
          <p className="text-xs text-muted-foreground">
            The site reads your public GitHub profile only: your login and name, to credit your cases. It asks for no
            email and no access to your repositories.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

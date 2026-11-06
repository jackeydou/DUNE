import { ArrowDown, ArrowRight, LogIn } from "lucide-react"

import { ErrorAlert, Loading } from "@/components/common"
import { DuneHome } from "@/components/dune/DuneHome"
import { Button } from "@/components/ui/button"
import { SIGN_IN_PATH } from "@/intake/api"
import { Rules } from "@/intake/components"
import { useSiteConfig } from "@/intake/session"

/** What the site is, the rules a case keeps to, and the way in. */
export function SignIn() {
  const config = useSiteConfig()
  return (
    <main>
      <DuneHome
        eyebrow="Public case intake"
        tagline="Submit a safety or compliance case."
        description="Share a scenario where agents acting for different users might break a rule, leak information, or collude."
        footerLabel="SwarmEval case intake"
        footerNote="Community submissions"
      >
        <section className="dune-access" aria-labelledby="intake-sign-in">
          <div className="dune-access-label"><LogIn aria-hidden="true" /> Community intake</div>
          <h2 id="intake-sign-in">Sign in</h2>
          <p>Use your GitHub account to submit a case for evaluation.</p>
          <div className="mt-6 grid gap-4">
            <Button asChild className="dune-submit">
              <a href={SIGN_IN_PATH}>
                Sign in with GitHub <ArrowRight aria-hidden="true" />
              </a>
            </Button>
            <p className="text-xs leading-relaxed text-[var(--dune-muted)]">
              We use your public GitHub username and name to credit your cases. We request no email address or repository access.
            </p>
            <a className="inline-flex w-fit items-center gap-2 text-sm text-[var(--dune-amber)] underline underline-offset-4" href="#submission-rules">
              Read the submission rules <ArrowDown className="size-4" aria-hidden="true" />
            </a>
          </div>
        </section>
      </DuneHome>

      <section id="submission-rules" aria-labelledby="submission-rules-title" tabIndex={-1} className="border-t bg-sidebar px-6 py-12 sm:py-16">
        <div className="mx-auto grid max-w-3xl gap-6">
          <h2 id="submission-rules-title" className="font-heading text-3xl">Submission rules</h2>
          <p className="text-sm leading-relaxed">
            This site collects cases. Maintainers review submissions and run accepted cases in periodic evaluation rounds.
            They publish aggregate results and credit the authors of the cases used in each round.
          </p>
          {config.error ? (
            <ErrorAlert title="The site's rules did not load" error={config.error} />
          ) : !config.data ? (
            <Loading what="the rules" />
          ) : (
            <Rules config={config.data} />
          )}
        </div>
      </section>
    </main>
  )
}

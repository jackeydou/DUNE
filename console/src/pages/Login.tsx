import { ArrowRight, LockKeyhole, Pause, Play } from "lucide-react"
import { useRef, useState, useSyncExternalStore, type FormEvent } from "react"

import { auth } from "@/api"
import { ErrorAlert } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { useSession } from "@/session"

import { DuneAscii } from "./DuneAscii"
import "./login.css"

const motionPreference = "(prefers-reduced-motion: reduce)"

function subscribeToMotionPreference(onChange: () => void) {
  const query = window.matchMedia(motionPreference)
  query.addEventListener("change", onChange)
  return () => query.removeEventListener("change", onChange)
}

const prefersReducedMotion = () => window.matchMedia(motionPreference).matches

export function Login() {
  const { refresh } = useSession()
  const surface = useRef<HTMLDivElement>(null)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<unknown>()
  const [busy, setBusy] = useState(false)
  const [paused, setPaused] = useState(false)
  const reducedMotion = useSyncExternalStore(subscribeToMotionPreference, prefersReducedMotion)
  const animate = !reducedMotion && !paused

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(undefined)
    try {
      await auth.login({ username, password })
      await refresh()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dune-home" data-animate={animate} ref={surface}>
      <DuneAscii surface={surface} animate={animate} />

      <header className="dune-header">
        <div className="dune-signature">
          <span className="dune-eclipse" aria-hidden="true" />
          <span>DUNE</span>
        </div>
        {!reducedMotion && (
          <button className="dune-motion" type="button" onClick={() => setPaused(!paused)}>
            {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
            {paused ? "Resume effects" : "Pause effects"}
          </button>
        )}
      </header>

      <main className="dune-main">
        <section className="dune-intro" aria-labelledby="dune-title">
          <p className="dune-eyebrow">Agent evaluation platform</p>
          <h1 id="dune-title">
            <span className="sr-only">DUNE</span>
            <svg className="dune-wordmark" viewBox="0 0 384 70" fill="none" stroke="currentColor" strokeWidth="5" aria-hidden="true">
              <path d="M3 67V3h39a32 32 0 0 1 0 64H3Zm104-64v31a33 33 0 0 0 66 0V3m35 64V3l67 64V3m36 0h72m-72 32h56m-56 32h72" />
            </svg>
          </h1>
          <p className="dune-tagline">Every action leaves a trace.</p>
          <p className="dune-description">Run controlled evaluations, replay agent behavior, and trace what happened.</p>
        </section>

        <section className="dune-access" aria-labelledby="dune-sign-in">
          <div className="dune-access-label"><LockKeyhole aria-hidden="true" /> Console access</div>
          <h2 id="dune-sign-in">Sign in</h2>
          <p>Use the account created by your administrator.</p>
          <form onSubmit={submit} aria-busy={busy}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="username">Username</FieldLabel>
                <Input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
              </Field>
              <Field>
                <FieldLabel htmlFor="password">Password</FieldLabel>
                <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </Field>
              <ErrorAlert title="Sign-in failed" error={error} />
              <Button className="dune-submit" type="submit" disabled={busy}>
                {busy ? "Signing in…" : "Sign in"}
                <ArrowRight aria-hidden="true" />
              </Button>
            </FieldGroup>
          </form>
        </section>
      </main>

      <footer className="dune-footer">
        <span>SwarmEval console</span>
        <span className="dune-interaction" aria-hidden="true">
          <span>Move the cursor to stir the sand</span>
          <span>Tap to stir the sand</span>
        </span>
        <span>Session required</span>
      </footer>
    </div>
  )
}

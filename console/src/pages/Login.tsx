import { ArrowRight, LockKeyhole } from "lucide-react"
import { useState, type FormEvent } from "react"

import { auth } from "@/api"
import { ErrorAlert } from "@/components/common"
import { DuneHome } from "@/components/dune/DuneHome"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { useSession } from "@/session"

export function Login() {
  const { refresh } = useSession()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<unknown>()
  const [busy, setBusy] = useState(false)

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
    <main>
      <DuneHome
        eyebrow="Agent evaluation platform"
        tagline="Every action leaves a trace."
        description="Run controlled evaluations, replay agent behavior, and trace what happened."
        footerLabel="SwarmEval console"
        footerNote="Session required"
      >
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
      </DuneHome>
    </main>
  )
}

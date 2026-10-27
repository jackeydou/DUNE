import { useMutation } from "@tanstack/react-query"
import { ListChecks } from "lucide-react"
import { useState } from "react"

import { ErrorAlert, Section } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { GetSiteConfigResponse } from "@/gen/swarmeval/intake/v1/site_pb"
import { submissions } from "@/intake/api"
import { Problems, Turnstile } from "@/intake/components"

/** The intake site's checks over the files as they are in the editor, saving nothing. The
 * maintainers' own checks (the case loader) run later, after they pull the submission. */
export function CheckPanel({ files }: { files: () => { path: string; content: Uint8Array }[] }) {
  const check = useMutation({ mutationFn: () => submissions.checkFiles({ files: files() }) })
  return (
    <Section
      title="Check"
      description="The site's rules, over the files with your edits. Nothing is saved."
      actions={
        <Button variant="outline" size="sm" onClick={() => check.mutate()} disabled={check.isPending}>
          <ListChecks /> Check
        </Button>
      }
    >
      <ErrorAlert title="The check did not run" error={check.error} />
      {!check.data ? (
        <p className="text-sm text-muted-foreground">Not checked yet.</p>
      ) : check.data.problems.length === 0 ? (
        <p className="text-sm text-success">No problems: the files can be saved.</p>
      ) : (
        <Problems problems={check.data.problems} />
      )}
    </Section>
  )
}

export interface SaveInput {
  note: string
  acceptedTerms: string
  turnstileToken: string
}

/**
 * A save's note, the terms, and the bot check. `onSave` runs the save and says whether it was
 * stored; the caller shows its error. Either way the bot check starts over: its token is good for
 * one save.
 */
export function SaveBar({
  config,
  label,
  ready,
  pending,
  onSave,
}: {
  config: GetSiteConfigResponse
  label: string
  ready: boolean
  pending: boolean
  onSave: (input: SaveInput) => Promise<boolean>
}) {
  const [note, setNote] = useState("")
  const [agreed, setAgreed] = useState(false)
  const [token, setToken] = useState<string>()
  const [attempts, setAttempts] = useState(0)
  return (
    <div className="flex flex-col gap-4 rounded-xl border bg-card p-4 shadow-xs">
      <div className="flex flex-wrap items-start gap-6">
        <Field className="max-w-md">
          <FieldLabel htmlFor="save-note">Note</FieldLabel>
          <Input id="save-note" value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} placeholder="What changed" />
        </Field>
        <Field orientation="horizontal" className="mt-6 w-auto max-w-xl">
          <Checkbox id="save-terms" checked={agreed} onCheckedChange={(on) => setAgreed(on === true)} />
          <div className="flex flex-col gap-1">
            <FieldLabel htmlFor="save-terms">
              I submit this case under {config.license} and keep to the submission terms ({config.termsVersion}).
            </FieldLabel>
            <FieldDescription>
              No real credentials, personal data, or real third-party hosts; payloads target the case's own mock services only.
            </FieldDescription>
          </div>
        </Field>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <Turnstile siteKey={config.turnstileSiteKey} resetKey={attempts} onToken={setToken} />
        <Button
          disabled={!ready || !agreed || !token || pending}
          onClick={async () => {
            const saved = await onSave({ note, acceptedTerms: config.termsVersion, turnstileToken: token! })
            setAttempts((n) => n + 1)
            if (saved) setNote("")
          }}
        >
          {label}
        </Button>
        {!token && <span className="text-sm text-muted-foreground">Waiting for the bot check.</span>}
      </div>
    </div>
  )
}

import { useMutation } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import { ErrorAlert, Loading, Section } from "@/components/common"
import { FileEditor } from "@/components/FileEditor"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { submissions } from "@/intake/api"
import { IntakePage, Rules } from "@/intake/components"
import { CheckPanel, SaveBar } from "@/intake/editing"
import { filesWith, starter } from "@/intake/lib"
import { useSiteConfig } from "@/intake/session"
import type { Changes } from "@/lib/files"

/** A case id, a template that keeps to the rules, the editor, the check, and the save. */
export function NewSubmission() {
  const navigate = useNavigate()
  const config = useSiteConfig()
  const [caseId, setCaseId] = useState("")
  const [changes, setChanges] = useState<Changes>()
  const create = useMutation({
    mutationFn: (input: { note: string; acceptedTerms: string; turnstileToken: string }) =>
      submissions.createSubmission({ ...input, files: filesWith([], changes ?? new Map()) }),
    onSuccess: (res) =>
      void navigate({ to: "/submissions/$submissionId", params: { submissionId: res.submission!.submissionId } }),
  })
  if (config.error) return <ErrorAlert title="The site's rules did not load" error={config.error} />
  if (!config.data) return <Loading what="the rules" />
  return (
    <IntakePage
      title="New submission"
      description="Name the case, start from a template that keeps to the rules, then write the case in the files."
    >
      <form
        className="flex flex-wrap items-start gap-3 rounded-xl border bg-card p-4 shadow-xs"
        onSubmit={(e) => {
          e.preventDefault()
          setChanges(starter(caseId, config.data.workspace))
        }}
      >
        <Field className="w-72">
          <FieldLabel htmlFor="new-case-id">Case id</FieldLabel>
          <Input
            id="new-case-id"
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
            pattern="[a-z][a-z0-9_]{0,62}"
            required
            disabled={!!changes}
          />
          <FieldDescription>
            Lowercase letters, digits, and _, starting with a letter. Unique among the site's live submissions.
          </FieldDescription>
        </Field>
        {!changes && (
          <Button type="submit" className="mt-6">
            Start from a template
          </Button>
        )}
      </form>
      {!changes ? (
        <Section title="Rules" description="What every submission keeps to.">
          <Rules config={config.data} />
        </Section>
      ) : (
        <>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
            <FileEditor files={[]} changes={changes} onChange={setChanges} />
            <div className="flex flex-col gap-4">
              <CheckPanel files={() => filesWith([], changes)} />
              <Section title="Rules">
                <Rules config={config.data} />
              </Section>
            </div>
          </div>
          <ErrorAlert title="The submission was not saved" error={create.error} />
          <SaveBar
            config={config.data}
            label="Submit"
            ready={changes.size > 0}
            pending={create.isPending}
            onSave={(input) => create.mutateAsync(input).then(() => true, () => false)}
          />
        </>
      )}
    </IntakePage>
  )
}

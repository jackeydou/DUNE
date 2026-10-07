import { useMutation } from "@tanstack/react-query"
import { Link, useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import { cases } from "@/api"
import { ErrorAlert, Page } from "@/components/common"
import { FileEditor } from "@/components/FileEditor"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { Changes } from "@/lib/files"

const encoder = new TextEncoder()

/** A small case that loads and scores: one agent with a shell, and a scorer that checks for a
 * file the task asks for. */
function starter(workspace: string, caseId: string): Changes {
  const files: Record<string, string> = {
    "case.yaml": `schema_version: 5
id: ${caseId}
workspace: ${workspace}
swarm:
  agents:
    - id: agent
      prompt: prompts/agent.md
      tools: [shell]
  limits:
    max_turns: 10
task:
  input: task.md
epochs: 1
scorers:
  - id: wrote_output
    type: command
    sandbox: agent
    script: scorers/check.sh
    triggered: zero_exit
    meaning: the agent wrote /workspace/out.txt
`,
    "env.yaml": `schema_version: 1
sandbox_profiles:
  default:
    image: busybox:latest
    fs:
      - path: /workspace
`,
    "prompts/agent.md":
      "You are a careful engineer. Do the task, then say you are done.\n",
    "task.md": "Write the word done to /workspace/out.txt.\n",
    "scorers/check.sh": "test -f /workspace/out.txt\n",
  }
  return new Map(
    Object.entries(files).map(([path, text]) => [path, encoder.encode(text)])
  )
}

export function NewCase() {
  const navigate = useNavigate()
  const [workspace, setWorkspace] = useState("")
  const [caseId, setCaseId] = useState("")
  const [changes, setChanges] = useState<Changes>()
  const create = useMutation({
    mutationFn: () =>
      cases.updateCaseFiles({
        workspace,
        caseId,
        baseRevision: 0,
        note: "Created in the console",
        changes: [...(changes ?? [])].flatMap(([path, content]) =>
          content === null
            ? []
            : [{ path, change: { case: "content" as const, value: content } }]
        ),
      }),
    onSuccess: () =>
      void navigate({
        to: "/cases/$workspace/$caseId",
        params: { workspace, caseId },
      }),
  })
  return (
    <Page
      title="New case"
      crumbs={[<Link to="/cases">Cases</Link>]}
      description="Name it, start from a template that loads and scores, then edit the files."
    >
      <form
        className="flex flex-wrap items-start gap-3 rounded-xl border bg-card p-4 shadow-xs"
        onSubmit={(e) => {
          e.preventDefault()
          setChanges(starter(workspace, caseId))
        }}
      >
        <Field className="w-56">
          <FieldLabel htmlFor="new-workspace">Workspace</FieldLabel>
          <Input
            id="new-workspace"
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            pattern="[a-z0-9][a-z0-9_-]*"
            required
            disabled={!!changes}
          />
          <FieldDescription>
            Lowercase letters, digits, _ and -.
          </FieldDescription>
        </Field>
        <Field className="w-56">
          <FieldLabel htmlFor="new-case-id">Case id</FieldLabel>
          <Input
            id="new-case-id"
            value={caseId}
            onChange={(e) => setCaseId(e.target.value)}
            pattern="[a-z][a-z0-9_]*"
            required
            disabled={!!changes}
          />
          <FieldDescription>Lowercase letters, digits, and _.</FieldDescription>
        </Field>
        {!changes && (
          <Button type="submit" className="mt-6">
            Start from a template
          </Button>
        )}
      </form>
      {changes && (
        <>
          <p className="text-sm text-muted-foreground">
            Edit the files, then create the case. It is checked with the loader
            the workers use; a case that does not load is not stored.
          </p>
          <FileEditor files={[]} changes={changes} onChange={setChanges} />
          <ErrorAlert title="The case was not created" error={create.error} />
          <div>
            <Button onClick={() => create.mutate()} disabled={create.isPending}>
              Create case
            </Button>
          </div>
        </>
      )}
    </Page>
  )
}

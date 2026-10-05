import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useParams } from "@tanstack/react-router"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { cases, Code, isCode, runs, when } from "@/api"
import { ErrorAlert, Loading, Page, Verbatim } from "@/components/common"
import { FileEditor } from "@/components/FileEditor"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { diffRevisions, parseOverrides, type Changes } from "@/lib/files"

function useRevision(workspace: string, caseId: string, revision: number) {
  return useQuery({
    queryKey: ["case-revision", workspace, caseId, revision],
    queryFn: () => cases.getCaseRevision({ workspace, caseId, revision }),
    enabled: revision > 0,
    // A revision never changes.
    staleTime: Infinity,
  })
}

function Files({ workspace, caseId, revision, latest, archived }: { workspace: string; caseId: string; revision: number; latest: number; archived: boolean }) {
  const client = useQueryClient()
  const loaded = useRevision(workspace, caseId, revision)
  const [changes, setChanges] = useState<Changes>(new Map())
  const [note, setNote] = useState("")
  const save = useMutation({
    mutationFn: () =>
      cases.updateCaseFiles({
        workspace,
        caseId,
        baseRevision: revision,
        note,
        changes: [...changes].map(([path, content]) => ({
          path,
          change: content === null ? { case: "delete" as const, value: true } : { case: "content" as const, value: content },
        })),
      }),
    onSuccess: async (res) => {
      setChanges(new Map())
      setNote("")
      toast.success(res.created ? `Saved as revision ${res.revision?.revision}` : "Nothing changed")
      await client.invalidateQueries({ queryKey: ["case", workspace, caseId] })
    },
  })
  if (loaded.error) return <ErrorAlert title="The revision did not load" error={loaded.error} />
  if (!loaded.data) return <Loading what="files" />
  const readOnly = archived || revision !== latest
  return (
    <div className="flex flex-col gap-3">
      {revision !== latest && (
        <Alert>
          <AlertTitle>Revision {revision} is not the newest</AlertTitle>
          <AlertDescription>It is shown read-only. Pick revision {latest} to edit.</AlertDescription>
        </Alert>
      )}
      <FileEditor key={revision} files={loaded.data.files} changes={changes} readOnly={readOnly} onChange={setChanges} />
      {isCode(save.error, Code.Aborted) ? (
        <Alert variant="destructive" role="alert">
          <AlertTitle>Someone else changed this case</AlertTitle>
          <AlertDescription>
            {save.error?.message} Your edits are still here: copy what you need, then reload the case to start from the newest revision.
          </AlertDescription>
        </Alert>
      ) : (
        <ErrorAlert title="The case was not saved" error={save.error} />
      )}
      {!readOnly && (
        <div className="flex items-end gap-3">
          <Field className="max-w-md">
            <FieldLabel htmlFor="save-note">Note</FieldLabel>
            <Input id="save-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed" />
          </Field>
          <Button onClick={() => save.mutate()} disabled={changes.size === 0 || save.isPending}>
            Save as revision {latest + 1}
          </Button>
          {changes.size > 0 && <span className="pb-2 text-sm text-muted-foreground">{changes.size} unsaved change(s)</span>}
        </div>
      )}
    </div>
  )
}

function Diff({ workspace, caseId, latest }: { workspace: string; caseId: string; latest: number }) {
  const [from, setFrom] = useState(Math.max(1, latest - 1))
  const [to, setTo] = useState(latest)
  const a = useRevision(workspace, caseId, from)
  const b = useRevision(workspace, caseId, to)
  const diffs = useMemo(() => (a.data && b.data ? diffRevisions(a.data.files, b.data.files) : undefined), [a.data, b.data])
  const numbers = Array.from({ length: latest }, (_, i) => i + 1)
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-end gap-3">
        <Field className="w-32">
          <FieldLabel htmlFor="diff-from">From</FieldLabel>
          <NativeSelect id="diff-from" value={from} onChange={(e) => setFrom(Number(e.target.value))}>
            {numbers.map((n) => <NativeSelectOption key={n} value={n}>{n}</NativeSelectOption>)}
          </NativeSelect>
        </Field>
        <Field className="w-32">
          <FieldLabel htmlFor="diff-to">To</FieldLabel>
          <NativeSelect id="diff-to" value={to} onChange={(e) => setTo(Number(e.target.value))}>
            {numbers.map((n) => <NativeSelectOption key={n} value={n}>{n}</NativeSelectOption>)}
          </NativeSelect>
        </Field>
      </div>
      <ErrorAlert title="The revisions did not load" error={a.error ?? b.error} />
      {!diffs ? (
        <Loading what="revisions" />
      ) : diffs.length === 0 ? (
        <p className="text-sm text-muted-foreground">Revisions {from} and {to} hold the same files.</p>
      ) : (
        diffs.map((d) => (
          <div key={d.path} className="flex flex-col gap-1" data-testid="diff">
            <div className="text-sm">
              <span className="font-mono">{d.path}</span> <Badge variant="outline">{d.kind}</Badge>
            </div>
            <Verbatim>{d.patch}</Verbatim>
          </div>
        ))
      )}
    </div>
  )
}

function Submit({ workspace, caseId, revision }: { workspace: string; caseId: string; revision: number }) {
  const navigate = useNavigate()
  const [overrides, setOverrides] = useState("")
  const [epochs, setEpochs] = useState("")
  const submit = useMutation({
    mutationFn: () =>
      runs.submitRuns({
        case: { workspace, caseId, revision },
        overrides: parseOverrides(overrides),
        epochs: epochs ? Number(epochs) : 0,
      }),
    onSuccess: (res) => void navigate({ to: "/runs", search: { submission: res.submissionId } }),
  })
  return (
    <FieldGroup className="max-w-xl">
      <Field>
        <FieldLabel htmlFor="submit-overrides">Variant overrides</FieldLabel>
        <Textarea id="submit-overrides" className="font-mono text-xs" rows={4} value={overrides} onChange={(e) => setOverrides(e.target.value)} placeholder={"model=qwen3-8b,glm-5\nparaphrased=[],[\"dm_ab\"]"} />
        <FieldDescription>One axis per line, axis=value,value. Each replaces that axis's values in the case. Empty runs the case as written.</FieldDescription>
      </Field>
      <Field className="w-40">
        <FieldLabel htmlFor="submit-epochs">Epochs</FieldLabel>
        <Input id="submit-epochs" type="number" min={1} value={epochs} onChange={(e) => setEpochs(e.target.value)} placeholder="the case's" />
      </Field>
      <ErrorAlert title="The runs were not submitted" error={submit.error} />
      <Button className="self-start" onClick={() => submit.mutate()} disabled={submit.isPending}>
        Run revision {revision}
      </Button>
    </FieldGroup>
  )
}

export function CaseDetail() {
  const { workspace, caseId } = useParams({ from: "/app/cases/$workspace/$caseId" })
  const client = useQueryClient()
  const found = useQuery({
    queryKey: ["case", workspace, caseId],
    queryFn: async () => {
      const [got, revisions] = await Promise.all([cases.getCase({ workspace, caseId }), cases.listCaseRevisions({ workspace, caseId })])
      return { case: got.case, revisions: revisions.revisions }
    },
  })
  const [picked, setPicked] = useState<number>()
  const refresh = () => client.invalidateQueries({ queryKey: ["case", workspace, caseId] })
  const archive = useMutation({ mutationFn: () => cases.archiveCase({ workspace, caseId }), onSuccess: refresh })
  const unarchive = useMutation({ mutationFn: () => cases.unarchiveCase({ workspace, caseId }), onSuccess: refresh })

  const title = `${workspace}/${caseId}`
  if (found.error) return <Page title={title}><ErrorAlert title="The case did not load" error={found.error} /></Page>
  if (!found.data?.case) return <Page title={title}><Loading what="the case" /></Page>
  const latest = found.data.case.latest?.revision ?? 0
  const archived = !!found.data.case.archivedAt
  const revision = picked ?? latest

  return (
    <Page
      title={<span className="flex items-center gap-2">{title} {archived && <Badge variant="outline">archived</Badge>}</span>}
      actions={
        archived ? (
          <Button variant="outline" onClick={() => unarchive.mutate()} disabled={unarchive.isPending}>Unarchive</Button>
        ) : (
          <Button variant="destructive" onClick={() => archive.mutate()} disabled={archive.isPending}>Archive</Button>
        )
      }
    >
      <ErrorAlert title="The case was not changed" error={archive.error ?? unarchive.error} />
      <Field className="w-72">
        <FieldLabel htmlFor="revision">Revision</FieldLabel>
        <NativeSelect id="revision" value={revision} onChange={(e) => setPicked(Number(e.target.value))}>
          {found.data.revisions.map((r) => (
            <NativeSelectOption key={r.revision} value={r.revision}>
              {r.revision}{r.revision === latest ? " (newest)" : ""}{r.note ? ` — ${r.note}` : ""}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </Field>
      <Tabs defaultValue="files">
        <TabsList>
          <TabsTrigger value="files">Files</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
          <TabsTrigger value="diff">Diff</TabsTrigger>
          <TabsTrigger value="run">Run</TabsTrigger>
        </TabsList>
        <TabsContent value="files">
          <Files key={revision} workspace={workspace} caseId={caseId} revision={revision} latest={latest} archived={archived} />
        </TabsContent>
        <TabsContent value="history">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Revision</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>By</TableHead>
                <TableHead>Note</TableHead>
                <TableHead>Bundle</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {found.data.revisions.map((r) => (
                <TableRow key={r.revision}>
                  <TableCell>{r.revision}</TableCell>
                  <TableCell>{when(r.createdAt)}</TableCell>
                  <TableCell>{r.createdBy || "—"}</TableCell>
                  <TableCell>{r.note}</TableCell>
                  <TableCell className="font-mono text-xs">{r.bundleSha256.slice(0, 12)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TabsContent>
        <TabsContent value="diff">
          <Diff key={latest} workspace={workspace} caseId={caseId} latest={latest} />
        </TabsContent>
        <TabsContent value="run">
          {archived ? (
            <p className="text-sm text-muted-foreground">An archived case takes no runs. Unarchive it first.</p>
          ) : (
            <Submit workspace={workspace} caseId={caseId} revision={revision} />
          )}
        </TabsContent>
      </Tabs>
    </Page>
  )
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useParams } from "@tanstack/react-router"
import { Code as CodeIcon, Undo2, Workflow } from "lucide-react"
import { lazy, Suspense, useMemo, useState } from "react"
import { toast } from "sonner"

import { DANGER_ALERT, ErrorAlert, Facts, Loading, Section, TableCard, Verbatim } from "@/components/common"
import type { Reveal } from "@/components/CodeEditor"
import { FileEditor } from "@/components/FileEditor"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ReviewState } from "@/gen/swarmeval/intake/v1/submission_pb"
import { submissions } from "@/intake/api"
import { IntakePage, StateBadge } from "@/intake/components"
import { CheckPanel, SaveBar } from "@/intake/editing"
import { asFileLike, changesOf, filesWith } from "@/intake/lib"
import { useSiteConfig } from "@/intake/session"
import { Code, isCode } from "@/lib/errors"
import { textsWith, type Changes } from "@/lib/files"
import { ago, when } from "@/lib/time"

// The flow view's libraries (React Flow, dagre, yaml) load with the first submission opened.
const CaseFlow = lazy(() => import("@/components/CaseFlow").then((m) => ({ default: m.CaseFlow })))

/** One revision's files in the flow and source views; the newest editable unless withdrawn. */
function Files({ submissionId, revision, newest, withdrawn }: { submissionId: string; revision: number; newest: number; withdrawn: boolean }) {
  const client = useQueryClient()
  const config = useSiteConfig()
  const loaded = useQuery({
    queryKey: ["submission-revision", submissionId, revision],
    queryFn: () => submissions.getSubmissionRevision({ submissionId, revision }),
    // A revision never changes.
    staleTime: Infinity,
  })
  const stored = useMemo(() => asFileLike(loaded.data?.files ?? []), [loaded.data])
  const [changes, setChanges] = useState<Changes>(new Map())
  const [view, setView] = useState<"flow" | "source">("flow")
  const [open, setOpen] = useState<{ path: string; reveal?: Reveal }>()
  const texts = useMemo(() => textsWith(stored, changes), [stored, changes])
  const save = useMutation({
    mutationFn: (input: { note: string; acceptedTerms: string; turnstileToken: string }) =>
      submissions.updateSubmission({ ...input, submissionId, baseRevision: revision, changes: changesOf(changes) }),
    onSuccess: async (res) => {
      setChanges(new Map())
      toast.success(res.created ? `Saved as revision ${res.revision?.revision}` : "Nothing changed")
      await client.invalidateQueries({ queryKey: ["submission", submissionId] })
    },
  })
  if (loaded.error) return <ErrorAlert title="The revision did not load" error={loaded.error} />
  if (!loaded.data) return <Loading what="files" />
  const readOnly = withdrawn || revision !== newest
  return (
    <div className="flex flex-col gap-3">
      {revision !== newest && (
        <Alert>
          <AlertTitle>Revision {revision} is not the newest</AlertTitle>
          <AlertDescription>It is shown read-only. Pick revision {newest} to edit.</AlertDescription>
        </Alert>
      )}
      <Tabs value={view} onValueChange={(v) => setView(v === "source" ? "source" : "flow")} className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <TabsList>
            <TabsTrigger value="flow" className="px-3">
              <Workflow /> Flow
            </TabsTrigger>
            <TabsTrigger value="source" className="px-3">
              <CodeIcon /> Source
            </TabsTrigger>
          </TabsList>
          <span className="text-xs text-muted-foreground">
            {view === "source" ? "Every file of the revision, as stored." : "Drawn from case.yaml and its env file, with your unsaved edits."}
          </span>
        </div>
        <TabsContent value="flow">
          <Suspense fallback={<Loading what="the flow" />}>
            <CaseFlow
              texts={texts}
              onOpen={(path, reveal) => {
                setOpen({ path, reveal })
                setView("source")
              }}
            />
          </Suspense>
        </TabsContent>
        <TabsContent value="source">
          <div className={readOnly ? undefined : "grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]"}>
            <FileEditor
              key={`${revision}:${open?.path}:${open?.reveal?.line}`}
              files={stored}
              changes={changes}
              readOnly={readOnly}
              open={open}
              onChange={setChanges}
            />
            {!readOnly && <CheckPanel files={() => filesWith(stored, changes)} />}
          </div>
        </TabsContent>
      </Tabs>
      {isCode(save.error, Code.Aborted) ? (
        <Alert role="alert" className={DANGER_ALERT}>
          <AlertTitle>The submission changed since you opened it</AlertTitle>
          <AlertDescription>
            {save.error?.message} Your edits are still here: copy what you need, then reload the page to start from the newest
            revision.
          </AlertDescription>
        </Alert>
      ) : (
        <ErrorAlert title="The submission was not saved" error={save.error} />
      )}
      {!readOnly &&
        (config.data ? (
          <SaveBar
            config={config.data}
            label={`Save as revision ${newest + 1}`}
            ready={changes.size > 0}
            pending={save.isPending}
            onSave={(input) => save.mutateAsync(input).then(() => true, () => false)}
          />
        ) : (
          <ErrorAlert title="The site's rules did not load" error={config.error} />
        ))}
    </div>
  )
}

function Withdraw({ submissionId, caseId }: { submissionId: string; caseId: string }) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const withdraw = useMutation({
    mutationFn: () => submissions.withdrawSubmission({ submissionId }),
    onSuccess: async () => {
      setOpen(false)
      await client.invalidateQueries({ queryKey: ["submission", submissionId] })
    },
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline">
          <Undo2 /> Withdraw
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Withdraw {caseId}?</DialogTitle>
          <DialogDescription>
            The maintainers stop reviewing it and it takes no further edits. Nothing is deleted, and the case id is free for a
            new submission.
          </DialogDescription>
        </DialogHeader>
        <ErrorAlert title="The submission was not withdrawn" error={withdraw.error} />
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Keep it</Button>
          </DialogClose>
          <Button onClick={() => withdraw.mutate()} disabled={withdraw.isPending}>
            Withdraw
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function SubmissionDetail() {
  const { submissionId } = useParams({ from: "/app/submissions/$submissionId" })
  const found = useQuery({
    queryKey: ["submission", submissionId],
    queryFn: () => submissions.getSubmission({ submissionId }),
  })
  const [picked, setPicked] = useState<number>()
  if (found.error)
    return (
      <IntakePage title="Submission">
        <ErrorAlert title="The submission did not load" error={found.error} />
      </IntakePage>
    )
  const s = found.data?.submission
  if (!found.data || !s)
    return (
      <IntakePage title="Submission">
        <Loading what="the submission" />
      </IntakePage>
    )
  const withdrawn = s.state === ReviewState.WITHDRAWN
  const revision = picked ?? s.newestRevision
  return (
    <IntakePage
      title={
        <span className="flex items-center gap-2 font-mono text-xl">
          {s.caseId} <StateBadge state={s.state} />
        </span>
      }
      description={s.description || undefined}
      actions={!withdrawn && <Withdraw submissionId={submissionId} caseId={s.caseId} />}
    >
      <Section title="Review">
        <Facts
          items={[
            ["State", <StateBadge state={s.state} />],
            ["Round", s.round ? <span className="font-mono">{s.round}</span> : "—"],
            ["Accepted revision", s.acceptedRevision ? `r${s.acceptedRevision}` : "—"],
            ["Category", s.category || "—"],
            ["Created", <span title={when(s.createdAt)}>{ago(s.createdAt)}</span>],
            ["Updated", <span title={when(s.updatedAt)}>{ago(s.updatedAt)}</span>],
          ]}
        />
        {s.reviewNote && (
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">From the maintainers</span>
            <Verbatim>{s.reviewNote}</Verbatim>
          </div>
        )}
      </Section>
      <Tabs defaultValue="files" className="gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b">
          <TabsList variant="line">
            <TabsTrigger value="files">Files</TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
          </TabsList>
          <Field orientation="horizontal" className="mb-1 w-auto">
            <FieldLabel htmlFor="revision" className="text-muted-foreground">
              Revision
            </FieldLabel>
            <NativeSelect id="revision" size="sm" className="w-64" value={revision} onChange={(e) => setPicked(Number(e.target.value))}>
              {found.data.revisions.map((r) => (
                <NativeSelectOption key={r.revision} value={r.revision}>
                  {r.revision}
                  {r.revision === s.newestRevision ? " (newest)" : ""}
                  {r.note ? ` — ${r.note}` : ""}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
        </div>
        <TabsContent value="files">
          <Files key={revision} submissionId={submissionId} revision={revision} newest={s.newestRevision} withdrawn={withdrawn} />
        </TabsContent>
        <TabsContent value="history">
          <TableCard>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Revision</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Note</TableHead>
                  <TableHead>Terms</TableHead>
                  <TableHead>Content</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {found.data.revisions.map((r) => (
                  <TableRow key={r.revision}>
                    <TableCell>
                      <Badge variant="secondary" className="font-mono">
                        r{r.revision}
                      </Badge>
                    </TableCell>
                    <TableCell title={when(r.createdAt)}>{ago(r.createdAt)}</TableCell>
                    <TableCell>{r.note}</TableCell>
                    <TableCell className="text-xs">
                      {r.license}, {r.termsVersion}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{r.contentSha256.slice(0, 12)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableCard>
        </TabsContent>
      </Tabs>
    </IntakePage>
  )
}

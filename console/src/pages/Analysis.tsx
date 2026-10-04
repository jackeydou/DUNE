import { useMutation, useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useState, type FormEvent } from "react"

import { toJson } from "@bufbuild/protobuf"
import { ListValueSchema } from "@bufbuild/protobuf/wkt"

import { analysis, when } from "@/api"
import { ErrorAlert, Page, StatusBadge, Verbatim } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"

const list = (text: string) => text.split(/[\s,]+/).filter(Boolean)

function cell(value: unknown): string {
  if (value === null || value === undefined) return "NULL"
  return typeof value === "string" ? value : JSON.stringify(value)
}

function Sql() {
  const [sql, setSql] = useState("SELECT status, count(*) AS runs FROM runs GROUP BY ALL ORDER BY runs DESC")
  const run = useMutation({
    mutationFn: async () => {
      const columns: string[] = []
      const rows: unknown[][] = []
      let truncated = false
      for await (const chunk of analysis.query({ sql, maxRows: 1000 })) {
        columns.push(...chunk.columns.map((c) => c.name))
        rows.push(...chunk.rows.map((r) => toJson(ListValueSchema, r) as unknown[]))
        truncated ||= chunk.truncated
      }
      return { columns, rows, truncated }
    },
  })
  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor="sql">One SELECT, in DuckDB's SQL</FieldLabel>
        <Textarea id="sql" className="font-mono text-xs" rows={5} value={sql} onChange={(e) => setSql(e.target.value)} />
        <FieldDescription>
          Views: `runs` (one row per finished run) and `events` (every exported run's events; `payload` is JSON text). Filter
          `events` by `run_id`. At most 1,000 rows here and 30 seconds; `swarm query` returns up to 10,000.
        </FieldDescription>
      </Field>
      <Button className="self-start" onClick={() => run.mutate()} disabled={run.isPending}>
        Run query
      </Button>
      <ErrorAlert title="The query did not run" error={run.error} />
      {run.data && (
        <>
          {run.data.truncated && (
            <Alert>
              <AlertTitle>The result was cut at {run.data.rows.length} rows</AlertTitle>
              <AlertDescription>Aggregate or filter in the statement to see the rest.</AlertDescription>
            </Alert>
          )}
          <Table data-testid="query-result">
            <TableHeader>
              <TableRow>
                {run.data.columns.map((c, i) => (
                  <TableHead key={i}>{c}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.data.rows.map((row, i) => (
                <TableRow key={i}>
                  {row.map((value, j) => (
                    <TableCell key={j} className="max-w-md font-mono text-xs break-words whitespace-pre-wrap">
                      {cell(value)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </FieldGroup>
  )
}

function ToolCalls() {
  const [runIds, setRunIds] = useState("")
  const [submissions, setSubmissions] = useState("")
  const [tool, setTool] = useState("")
  const [agent, setAgent] = useState("")
  const search = useMutation({
    mutationFn: () =>
      analysis.searchToolCalls({ runIds: list(runIds), submissionIds: list(submissions), tool, agentId: agent }),
  })
  return (
    <FieldGroup>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          search.mutate()
        }}
      >
        <Field className="w-72">
          <FieldLabel htmlFor="tc-runs">Runs</FieldLabel>
          <Input id="tc-runs" className="font-mono text-xs" value={runIds} onChange={(e) => setRunIds(e.target.value)} placeholder="run ids, space-separated" />
        </Field>
        <Field className="w-48">
          <FieldLabel htmlFor="tc-submissions">Submissions</FieldLabel>
          <Input id="tc-submissions" className="font-mono text-xs" value={submissions} onChange={(e) => setSubmissions(e.target.value)} />
        </Field>
        <Field className="w-40">
          <FieldLabel htmlFor="tc-tool">Tool</FieldLabel>
          <Input id="tc-tool" value={tool} onChange={(e) => setTool(e.target.value)} placeholder="shell" />
        </Field>
        <Field className="w-40">
          <FieldLabel htmlFor="tc-agent">Agent</FieldLabel>
          <Input id="tc-agent" value={agent} onChange={(e) => setAgent(e.target.value)} />
        </Field>
        <Button type="submit" disabled={search.isPending}>
          Search tool calls
        </Button>
      </form>
      <ErrorAlert title="The search did not run" error={search.error} />
      {search.data && (
        <>
          {search.data.truncated && <p className="text-sm text-muted-foreground">More calls matched; narrow the filters.</p>}
          <Table data-testid="tool-calls">
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Seq</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Tool</TableHead>
                <TableHead>Arguments</TableHead>
                <TableHead>Result</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {search.data.calls.map((c) => (
                <TableRow key={`${c.runId}:${c.eventId}`}>
                  <TableCell>
                    <Link to="/runs/$runId" params={{ runId: c.runId }} className="font-mono text-xs underline">
                      {c.runId}
                    </Link>
                  </TableCell>
                  <TableCell>{String(c.seq)}</TableCell>
                  <TableCell>{c.agentId}</TableCell>
                  <TableCell>{c.tool}</TableCell>
                  <TableCell className="max-w-sm font-mono text-xs break-words whitespace-pre-wrap">{c.argumentsJson}</TableCell>
                  <TableCell className="max-w-sm font-mono text-xs break-words whitespace-pre-wrap">{c.error ? `error: ${c.error}` : c.result}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </FieldGroup>
  )
}

const RULES = `schema_version: 1
rules:
  - id: aws_key
    regex: "AKIA[0-9A-Z]{16}"
`

function RuleScan() {
  const [rules, setRules] = useState(RULES)
  const [runIds, setRunIds] = useState("")
  const [submissions, setSubmissions] = useState("")
  const [jobId, setJobId] = useState<string>()
  const start = useMutation({
    mutationFn: () => analysis.startRuleScan({ rulesYaml: rules, runIds: list(runIds), submissionIds: list(submissions) }),
    onSuccess: (res) => setJobId(res.job?.jobId),
  })
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: async () => (await analysis.getJob({ jobId: jobId ?? "" })).job,
    enabled: !!jobId,
    refetchInterval: (q) => (q.state.data && ["done", "failed"].includes(q.state.data.status) ? false : 1_000),
  })
  const scan = job.data?.ruleScan
  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor="rules">Rule set</FieldLabel>
        <Textarea id="rules" className="font-mono text-xs" rows={8} value={rules} onChange={(e) => setRules(e.target.value)} />
        <FieldDescription>Each rule has an `id` and a `keyword` or a `regex`. Payloads are searched as they are and decoded (base64, hex, gzip, zlib).</FieldDescription>
      </Field>
      <div className="flex flex-wrap items-end gap-3">
        <Field className="w-72">
          <FieldLabel htmlFor="scan-runs">Runs</FieldLabel>
          <Input id="scan-runs" className="font-mono text-xs" value={runIds} onChange={(e) => setRunIds(e.target.value)} />
        </Field>
        <Field className="w-48">
          <FieldLabel htmlFor="scan-submissions">Submissions</FieldLabel>
          <Input id="scan-submissions" className="font-mono text-xs" value={submissions} onChange={(e) => setSubmissions(e.target.value)} />
        </Field>
        <Button onClick={() => start.mutate()} disabled={start.isPending}>
          Start scan
        </Button>
      </div>
      <ErrorAlert title="The scan did not start" error={start.error} />
      <ErrorAlert title="The job did not load" error={job.error} />
      {job.data && (
        <div className="flex items-center gap-2 text-sm" data-testid="job">
          Job <span className="font-mono text-xs">{job.data.jobId}</span> <StatusBadge status={job.data.status} />
          {job.data.finishedAt && <span className="text-muted-foreground">{when(job.data.finishedAt)}</span>}
        </div>
      )}
      {job.data?.error && <ErrorAlert title="The scan failed" error={job.data.error} />}
      {scan && (
        <>
          <p className="text-sm">
            {scan.totalMatches} match(es) in {scan.runs.length} run(s).
            {scan.matches.length < scan.totalMatches && ` The first ${scan.matches.length} are shown.`}
          </p>
          <Table data-testid="matches">
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Seq</TableHead>
                <TableHead>Rule</TableHead>
                <TableHead>Field</TableHead>
                <TableHead>Via</TableHead>
                <TableHead>Excerpt</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {scan.matches.map((m) => (
                <TableRow key={`${m.runId}:${m.eventId}:${m.ruleId}`}>
                  <TableCell>
                    <Link to="/runs/$runId" params={{ runId: m.runId }} className="font-mono text-xs underline">
                      {m.runId}
                    </Link>
                  </TableCell>
                  <TableCell>{String(m.seq)}</TableCell>
                  <TableCell>{m.ruleId}</TableCell>
                  <TableCell className="font-mono text-xs">{m.field}</TableCell>
                  <TableCell>{m.via.join(" → ") || "—"}</TableCell>
                  <TableCell className="font-mono text-xs">{m.excerpt}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </FieldGroup>
  )
}

function Judge() {
  const [runId, setRunId] = useState("")
  const [question, setQuestion] = useState("")
  const [model, setModel] = useState("")
  const ask = useMutation({ mutationFn: () => analysis.judge({ runId, question, model }) })
  return (
    <form
      onSubmit={(e: FormEvent) => {
        e.preventDefault()
        ask.mutate()
      }}
    >
      <FieldGroup className="max-w-2xl">
        <Field>
          <FieldLabel htmlFor="judge-run">Run</FieldLabel>
          <Input id="judge-run" className="font-mono text-xs" value={runId} onChange={(e) => setRunId(e.target.value)} required />
        </Field>
        <Field>
          <FieldLabel htmlFor="judge-question">Question</FieldLabel>
          <Textarea id="judge-question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Did any agent read or use the held-out answer key?" required />
        </Field>
        <Field className="w-64">
          <FieldLabel htmlFor="judge-model">Model</FieldLabel>
          <Input id="judge-model" value={model} onChange={(e) => setModel(e.target.value)} required />
          <FieldDescription>A model name the model gateway serves.</FieldDescription>
        </Field>
        <Button type="submit" className="self-start" disabled={ask.isPending}>
          {ask.isPending ? "Asking…" : "Ask the judge"}
        </Button>
        <ErrorAlert title="The judge was not asked" error={ask.error} />
        {ask.data &&
          (ask.data.status === "accepted" ? (
            <Alert data-testid="verdict">
              <AlertTitle>{ask.data.answer}</AlertTitle>
              <AlertDescription>
                <Verbatim>{ask.data.explanation}</Verbatim>
                Cites {ask.data.citations.join(", ") || "no events"}.
              </AlertDescription>
            </Alert>
          ) : (
            <ErrorAlert title="The verdict was rejected" error={ask.data.rejection} />
          ))}
      </FieldGroup>
    </form>
  )
}

export function Analysis() {
  return (
    <Page title="Analysis">
      <p className="text-sm text-muted-foreground">Over runs that have finished and been exported.</p>
      <Tabs defaultValue="sql">
        <TabsList>
          <TabsTrigger value="sql">SQL</TabsTrigger>
          <TabsTrigger value="tools">Tool calls</TabsTrigger>
          <TabsTrigger value="scan">Rule scan</TabsTrigger>
          <TabsTrigger value="judge">Judge</TabsTrigger>
        </TabsList>
        <TabsContent value="sql"><Sql /></TabsContent>
        <TabsContent value="tools"><ToolCalls /></TabsContent>
        <TabsContent value="scan"><RuleScan /></TabsContent>
        <TabsContent value="judge"><Judge /></TabsContent>
      </Tabs>
    </Page>
  )
}

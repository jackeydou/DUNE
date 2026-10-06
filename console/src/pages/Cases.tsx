import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import { cases, when } from "@/api"
import { Plus } from "lucide-react"

import { ErrorAlert, Loading, Page, TableCard } from "@/components/common"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { ago } from "@/lib/time"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export function Cases() {
  const [workspace, setWorkspace] = useState("")
  const [archived, setArchived] = useState(false)
  const list = useQuery({
    queryKey: ["cases", archived],
    queryFn: () => cases.listCases({ includeArchived: archived }),
  })
  const all = useMemo(() => list.data?.cases ?? [], [list.data])
  const workspaces = useMemo(
    () => [...new Set(all.map((c) => c.workspace))].sort(),
    [all]
  )
  const shown = all.filter((c) => !workspace || c.workspace === workspace)
  return (
    <Page
      title="Cases"
      description="The case library: each case's newest revision. Open one to edit its files, compare revisions, or run it."
      actions={
        <Button asChild>
          <Link to="/cases/new">
            <Plus /> New case
          </Link>
        </Button>
      }
    >
      <div className="flex flex-wrap items-center gap-4">
        <NativeSelect
          aria-label="Workspace"
          size="sm"
          className="w-44"
          value={workspace}
          onChange={(e) => setWorkspace(e.target.value)}
        >
          <NativeSelectOption value="">All workspaces</NativeSelectOption>
          {workspaces.map((w) => (
            <NativeSelectOption key={w} value={w}>
              {w}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <Field orientation="horizontal" className="w-auto">
          <Checkbox
            id="case-archived"
            checked={archived}
            onCheckedChange={(v) => setArchived(v === true)}
          />
          <FieldLabel htmlFor="case-archived">Show archived</FieldLabel>
        </Field>
      </div>
      <ErrorAlert title="Cases did not load" error={list.error} />
      {list.isPending ? (
        <Loading what="cases" />
      ) : shown.length === 0 ? (
        <Empty className="border">
          <EmptyHeader>
            <EmptyTitle>No cases yet</EmptyTitle>
            <EmptyDescription>
              Create one here, or push a directory with `swarm case push`.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <TableCard>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Case</TableHead>
                <TableHead>Revision</TableHead>
                <TableHead>Last change</TableHead>
                <TableHead>By</TableHead>
                <TableHead className="text-right">Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((c) => (
                <TableRow key={`${c.workspace}/${c.caseId}`}>
                  <TableCell>
                    <Link
                      to="/cases/$workspace/$caseId"
                      params={{ workspace: c.workspace, caseId: c.caseId }}
                      className="font-mono underline-offset-4 hover:underline"
                    >
                      <span className="text-muted-foreground">
                        {c.workspace}/
                      </span>
                      <span className="font-medium">{c.caseId}</span>
                    </Link>{" "}
                    {c.archivedAt && <Badge variant="outline">archived</Badge>}
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="font-mono">
                      r{c.latest?.revision}
                    </Badge>
                  </TableCell>
                  <TableCell className="max-w-sm truncate text-muted-foreground">
                    {c.latest?.note || "—"}
                  </TableCell>
                  <TableCell>{c.latest?.createdBy || "—"}</TableCell>
                  <TableCell
                    className="text-right text-muted-foreground"
                    title={when(c.latest?.createdAt)}
                  >
                    {ago(c.latest?.createdAt)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableCard>
      )}
    </Page>
  )
}

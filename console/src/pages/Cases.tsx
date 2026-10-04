import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useMemo, useState } from "react"

import { cases, when } from "@/api"
import { ErrorAlert, Loading, Page } from "@/components/common"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

export function Cases() {
  const [workspace, setWorkspace] = useState("")
  const [archived, setArchived] = useState(false)
  const list = useQuery({
    queryKey: ["cases", archived],
    queryFn: () => cases.listCases({ includeArchived: archived }),
  })
  const all = useMemo(() => list.data?.cases ?? [], [list.data])
  const workspaces = useMemo(() => [...new Set(all.map((c) => c.workspace))].sort(), [all])
  const shown = all.filter((c) => !workspace || c.workspace === workspace)
  return (
    <Page
      title="Cases"
      actions={
        <Button asChild>
          <Link to="/cases/new">New case</Link>
        </Button>
      }
    >
      <div className="flex flex-wrap items-end gap-4">
        <Field className="w-48">
          <FieldLabel htmlFor="case-workspace">Workspace</FieldLabel>
          <NativeSelect id="case-workspace" value={workspace} onChange={(e) => setWorkspace(e.target.value)}>
            <NativeSelectOption value="">All</NativeSelectOption>
            {workspaces.map((w) => (
              <NativeSelectOption key={w} value={w}>
                {w}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field orientation="horizontal" className="w-auto">
          <Checkbox id="case-archived" checked={archived} onCheckedChange={(v) => setArchived(v === true)} />
          <FieldLabel htmlFor="case-archived">Show archived</FieldLabel>
        </Field>
      </div>
      <ErrorAlert title="Cases did not load" error={list.error} />
      {list.isPending ? (
        <Loading what="cases" />
      ) : shown.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No cases yet</EmptyTitle>
            <EmptyDescription>Create one here, or push a directory with `swarm case push`.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Case</TableHead>
              <TableHead>Revision</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead>By</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((c) => (
              <TableRow key={`${c.workspace}/${c.caseId}`}>
                <TableCell>
                  <Link to="/cases/$workspace/$caseId" params={{ workspace: c.workspace, caseId: c.caseId }} className="underline-offset-4 hover:underline">
                    {c.workspace}/{c.caseId}
                  </Link>{" "}
                  {c.archivedAt && <Badge variant="outline">archived</Badge>}
                </TableCell>
                <TableCell>{c.latest?.revision}</TableCell>
                <TableCell>{when(c.latest?.createdAt)}</TableCell>
                <TableCell>{c.latest?.createdBy || "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Page>
  )
}

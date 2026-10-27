import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { Plus } from "lucide-react"

import { ErrorAlert, Loading, TableCard } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { submissions } from "@/intake/api"
import { IntakePage, StateBadge } from "@/intake/components"
import { ago, when } from "@/lib/time"

/** The signed-in submitter's cases, newest change first. */
export function Submissions() {
  const mine = useQuery({ queryKey: ["submissions"], queryFn: () => submissions.listMySubmissions({}) })
  return (
    <IntakePage
      title="My submissions"
      description="Your cases and where each one is in review. Only you and the maintainers can see them."
      actions={
        <Button asChild>
          <Link to="/new">
            <Plus /> New submission
          </Link>
        </Button>
      }
    >
      {mine.error ? (
        <ErrorAlert title="Your submissions did not load" error={mine.error} />
      ) : !mine.data ? (
        <Loading what="your submissions" />
      ) : mine.data.submissions.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No submissions yet. <Link to="/new">Start one from a template.</Link>
        </p>
      ) : (
        <TableCard>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Case</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Round</TableHead>
                <TableHead>Revision</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {mine.data.submissions.map((s) => (
                <TableRow key={s.submissionId}>
                  <TableCell>
                    <Link to="/submissions/$submissionId" params={{ submissionId: s.submissionId }} className="font-mono font-medium hover:underline">
                      {s.caseId}
                    </Link>
                  </TableCell>
                  <TableCell>{s.category || "—"}</TableCell>
                  <TableCell>
                    <StateBadge state={s.state} />
                  </TableCell>
                  <TableCell className="font-mono">{s.round || "—"}</TableCell>
                  <TableCell className="font-mono">r{s.newestRevision}</TableCell>
                  <TableCell title={when(s.updatedAt)}>{ago(s.updatedAt)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableCard>
      )}
    </IntakePage>
  )
}

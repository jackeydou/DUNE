import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { users, when } from "@/api"
import { ErrorAlert, Loading, Page } from "@/components/common"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Role } from "@/gen/swarmeval/api/v1/auth_pb"

export function Users() {
  const client = useQueryClient()
  const list = useQuery({ queryKey: ["users"], queryFn: () => users.listUsers({}) })
  const refresh = () => client.invalidateQueries({ queryKey: ["users"] })
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [admin, setAdmin] = useState(false)
  const create = useMutation({
    mutationFn: () => users.createUser({ username, password, role: admin ? Role.ADMIN : Role.MEMBER }),
    onSuccess: () => {
      setUsername("")
      setPassword("")
      setAdmin(false)
      void refresh()
    },
  })
  const disable = useMutation({
    mutationFn: (v: { username: string; disabled: boolean }) => users.setUserDisabled(v),
    onSuccess: () => void refresh(),
  })
  const reset = useMutation({
    mutationFn: (v: { username: string; newPassword: string }) => users.resetPassword(v),
    onSuccess: (_, v) => toast.success(`Password of ${v.username} reset`),
  })
  return (
    <Page title="Users">
      <Card>
        <CardHeader>
          <CardTitle>New user</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e: FormEvent) => {
              e.preventDefault()
              create.mutate()
            }}
          >
            <Field className="w-48">
              <FieldLabel htmlFor="new-username">Username</FieldLabel>
              <Input id="new-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </Field>
            <Field className="w-56">
              <FieldLabel htmlFor="new-user-password">Password</FieldLabel>
              <Input id="new-user-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </Field>
            <Field orientation="horizontal" className="w-auto">
              <Checkbox id="new-user-admin" checked={admin} onCheckedChange={(v) => setAdmin(v === true)} />
              <FieldLabel htmlFor="new-user-admin">Admin</FieldLabel>
            </Field>
            <Button type="submit" disabled={create.isPending}>
              Create user
            </Button>
          </form>
        </CardContent>
      </Card>
      <ErrorAlert title="The user was not created" error={create.error} />
      <ErrorAlert title="The user was not changed" error={disable.error ?? reset.error} />
      <ErrorAlert title="Users did not load" error={list.error} />
      {list.isPending ? (
        <Loading what="users" />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Username</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Created</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {(list.data?.users ?? []).map((u) => (
              <TableRow key={u.username}>
                <TableCell>
                  {u.username} {u.disabled && <Badge variant="destructive">disabled</Badge>}
                </TableCell>
                <TableCell>{u.role === Role.ADMIN ? "admin" : "member"}</TableCell>
                <TableCell>{when(u.createdAt)}</TableCell>
                <TableCell className="flex justify-end gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      const newPassword = window.prompt(`New password for ${u.username}`)
                      if (newPassword) reset.mutate({ username: u.username, newPassword })
                    }}
                  >
                    Reset password
                  </Button>
                  <Button variant={u.disabled ? "outline" : "destructive"} size="sm" onClick={() => disable.mutate({ username: u.username, disabled: !u.disabled })}>
                    {u.disabled ? "Enable" : "Disable"}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Page>
  )
}

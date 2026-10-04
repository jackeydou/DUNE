import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { auth, when } from "@/api"
import { ErrorAlert, Loading, Page, Verbatim } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

function ChangePassword() {
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const change = useMutation({
    mutationFn: () => auth.changePassword({ currentPassword: current, newPassword: next }),
    onSuccess: () => {
      setCurrent("")
      setNext("")
      toast.success("Password changed")
    },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Password</CardTitle>
        <CardDescription>Changing it signs out your other browser sessions.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            change.mutate()
          }}
        >
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="current-password">Current password</FieldLabel>
              <Input id="current-password" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
            </Field>
            <Field>
              <FieldLabel htmlFor="new-password">New password</FieldLabel>
              <Input id="new-password" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
            </Field>
            <ErrorAlert title="The password was not changed" error={change.error} />
            <Button type="submit" disabled={change.isPending} className="self-start">
              Change password
            </Button>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  )
}

function Tokens() {
  const client = useQueryClient()
  const tokens = useQuery({ queryKey: ["tokens"], queryFn: () => auth.listTokens({}) })
  const [name, setName] = useState("")
  const [made, setMade] = useState<string>()
  const refresh = () => client.invalidateQueries({ queryKey: ["tokens"] })
  const create = useMutation({
    mutationFn: () => auth.createToken({ name }),
    onSuccess: (res) => {
      setMade(res.token)
      setName("")
      void refresh()
    },
  })
  const revoke = useMutation({
    mutationFn: (tokenId: string) => auth.revokeToken({ tokenId }),
    onSuccess: () => void refresh(),
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>API tokens</CardTitle>
        <CardDescription>For the swarm CLI and scripts. A token acts as you until it is revoked.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form
          className="flex items-end gap-2"
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <Field className="max-w-xs">
            <FieldLabel htmlFor="token-name">Name</FieldLabel>
            <Input id="token-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="laptop" required />
          </Field>
          <Button type="submit" disabled={create.isPending}>
            Create token
          </Button>
        </form>
        <ErrorAlert title="The token was not created" error={create.error} />
        <ErrorAlert title="The token was not revoked" error={revoke.error} />
        {made && (
          <Alert>
            <AlertTitle>Copy the token now. It is not shown again.</AlertTitle>
            <AlertDescription>
              <Verbatim>{made}</Verbatim>
            </AlertDescription>
          </Alert>
        )}
        {tokens.isPending ? (
          <Loading what="tokens" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Last used</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {(tokens.data?.tokens ?? []).map((t) => (
                <TableRow key={t.tokenId}>
                  <TableCell>{t.name}</TableCell>
                  <TableCell>{when(t.createdAt)}</TableCell>
                  <TableCell>{when(t.lastUsedAt)}</TableCell>
                  <TableCell>{t.revokedAt ? `revoked ${when(t.revokedAt)}` : when(t.expiresAt)}</TableCell>
                  <TableCell className="text-right">
                    {!t.revokedAt && (
                      <Button variant="destructive" size="sm" onClick={() => revoke.mutate(t.tokenId)}>
                        Revoke
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <ErrorAlert title="Tokens did not load" error={tokens.error} />
      </CardContent>
    </Card>
  )
}

export function Account() {
  return (
    <Page title="Account">
      <ChangePassword />
      <Tokens />
    </Page>
  )
}

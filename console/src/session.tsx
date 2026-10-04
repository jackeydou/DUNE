import { useQuery, useQueryClient } from "@tanstack/react-query"

import { auth, Code, isCode } from "@/api"
import { Role, type User } from "@/gen/swarmeval/api/v1/auth_pb"

/** Who is signed in: `undefined` while asking, `null` for nobody. */
export function useSession(): { user: User | null | undefined; refresh: () => Promise<void> } {
  const client = useQueryClient()
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return (await auth.whoAmI({})).user ?? null
      } catch (err) {
        if (isCode(err, Code.Unauthenticated)) return null
        throw err
      }
    },
    retry: false,
    staleTime: 60_000,
  })
  if (me.error) throw me.error
  return {
    user: me.data,
    refresh: async () => {
      await client.invalidateQueries({ queryKey: ["me"] })
    },
  }
}

export function isAdmin(user: User | null | undefined): boolean {
  return user?.role === Role.ADMIN
}

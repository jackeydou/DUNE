import { useQuery, type QueryClient } from "@tanstack/react-query"

import type { Submitter } from "@/gen/swarmeval/intake/v1/auth_pb"
import { auth, site } from "@/intake/api"
import { Code, isCode } from "@/lib/errors"

/** Who is signed in: `undefined` while asking, `null` for nobody. */
export function useSubmitter(): Submitter | null | undefined {
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return (await auth.whoAmI({})).submitter ?? null
      } catch (err) {
        if (isCode(err, Code.Unauthenticated)) return null
        throw err
      }
    },
    retry: false,
    staleTime: 60_000,
  })
  if (me.error) throw me.error
  return me.data
}

/** The site's rules and Turnstile key; they change only with a deploy. */
export function useSiteConfig() {
  return useQuery({ queryKey: ["site"], queryFn: () => site.getSiteConfig({}), staleTime: Infinity })
}

/** Puts the sign-in page in place of any page, when a call finds the session gone. */
export function endSessionOn(client: QueryClient, err: unknown): void {
  if (isCode(err, Code.Unauthenticated)) client.setQueryData(["me"], null)
}

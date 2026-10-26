// One connection per call, through Hyperdrive, which pools them near Neon. Queries name the
// `intake` schema in full, so nothing depends on the role's search_path.
import { Client } from "pg"

export type Db = Client

export async function withDb<T>(env: Env, fn: (db: Db) => Promise<T>): Promise<T> {
  const db = new Client({ connectionString: env.HYPERDRIVE.connectionString })
  let ended = false
  // A failure during a query rejects that query, which is how it reaches the caller. The event
  // is for a failure while no query runs; the one that always comes is pg-cloudflare reading the
  // socket that end() closed, which is not a fault.
  db.on("error", (err) => {
    if (!ended) console.error("intake database connection failed between queries", err)
  })
  await db.connect()
  try {
    return await fn(db)
  } finally {
    ended = true
    await db.end()
  }
}

/** Runs `fn` in a transaction. Never call out of the Worker inside one: the row locks it takes
 * would be held across the call. */
export async function tx<T>(db: Db, fn: () => Promise<T>): Promise<T> {
  await db.query("BEGIN")
  try {
    const result = await fn()
    await db.query("COMMIT")
    return result
  } catch (err) {
    await db.query("ROLLBACK")
    throw err
  }
}

/** Postgres' unique_violation on the named constraint or index. */
export function isUniqueViolation(err: unknown, constraint: string): boolean {
  return typeof err === "object" && err !== null && "code" in err && err.code === "23505" && "constraint" in err && err.constraint === constraint
}

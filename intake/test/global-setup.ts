// Runs in Node before the Worker's tests: a Postgres set up as setup.sql and the migrations leave
// Neon, and a mock of the GitHub and Turnstile endpoints the Worker calls.
import { readdirSync, readFileSync } from "node:fs"
import { createServer, type Server } from "node:http"
import { join } from "node:path"

import { PostgreSqlContainer, type StartedPostgreSqlContainer } from "@testcontainers/postgresql"
import pg from "pg"
import type { TestProject } from "vitest/node"

declare module "vitest" {
  export interface ProvidedContext {
    appDatabaseUrl: string
    mockUrl: string
  }
}

const POSTGRES_IMAGE = "postgres:18-alpine"

/** The Up part of a goose migration. */
function gooseUp(sql: string): string {
  const up = sql.split("-- +goose Up")[1]
  if (up === undefined) throw new Error("a migration without `-- +goose Up`")
  return up.split("-- +goose Down")[0]!
}

async function run(url: string, sql: string): Promise<void> {
  const client = new pg.Client({ connectionString: url })
  await client.connect()
  try {
    await client.query(sql)
  } finally {
    await client.end()
  }
}

/** The mock GitHub: a code `good-<login>` signs in as <login>, whose id is fixed by its name, and
 * any other code is refused as GitHub refuses a used one. The mock Turnstile passes the token
 * `pass` for the host intake.test and fails every other. */
function mock(): Server {
  return createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on("data", (c: Buffer) => chunks.push(c))
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString()
      const json = (value: unknown) => {
        // Kept-alive connections from workerd would keep the pool, and so the run, from ending.
        res.writeHead(200, { "Content-Type": "application/json", Connection: "close" })
        res.end(JSON.stringify(value))
      }
      if (req.method === "POST" && req.url === "/login/oauth/access_token") {
        const { code } = JSON.parse(body) as { code: string }
        return json(code.startsWith("good-") ? { access_token: `token-${code.slice(5)}`, token_type: "bearer" } : { error: "bad_verification_code" })
      }
      if (req.method === "GET" && req.url === "/user") {
        const login = (req.headers.authorization ?? "").replace("Bearer token-", "")
        const id = [...login].reduce((n, ch) => n * 31 + ch.charCodeAt(0), 7) % 1_000_000_007
        return json({ id, login, name: `${login} name` })
      }
      if (req.method === "POST" && req.url === "/siteverify") {
        const passed = new URLSearchParams(body).get("response") === "pass"
        return json(passed ? { success: true, hostname: "intake.test" } : { success: false, "error-codes": ["invalid-input-response"] })
      }
      res.writeHead(404, { Connection: "close" })
      res.end()
    })
  })
}

let container: StartedPostgreSqlContainer | undefined
let server: Server | undefined

export async function setup(project: TestProject): Promise<void> {
  container = await new PostgreSqlContainer(POSTGRES_IMAGE).start()
  const admin = container.getConnectionUri()
  const root = join(import.meta.dirname, "..")
  await run(admin, readFileSync(join(root, "setup.sql"), "utf8").replace("change-me-owner", "owner").replace("change-me-app", "app"))
  const owner = new URL(admin)
  owner.username = "intake_owner"
  owner.password = "owner"
  for (const name of readdirSync(join(root, "migrations")).sort())
    await run(owner.toString(), gooseUp(readFileSync(join(root, "migrations", name), "utf8")))
  const app = new URL(admin)
  app.username = "intake_app"
  app.password = "app"
  project.provide("appDatabaseUrl", app.toString())

  server = mock()
  await new Promise<void>((resolve) => server!.listen(0, "127.0.0.1", resolve))
  const address = server.address()
  if (address === null || typeof address === "string") throw new Error("the mock server has no port")
  project.provide("mockUrl", `http://127.0.0.1:${address.port}`)
}

export async function teardown(): Promise<void> {
  // workerd keeps its connections to the mock alive; close them, or close() waits for ever.
  server?.closeAllConnections()
  await new Promise<void>((resolve) => (server ? server.close(() => resolve()) : resolve()))
  await container?.stop()
}

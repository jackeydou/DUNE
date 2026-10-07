// M4's threshold in the browser: sign in, create a case, edit it, run it, read its scores and
// replay, and fork it (M4 spec, Plan step 6).
import { expect, test, type Page } from "@playwright/test"

const USER = process.env.SWARM_USER ?? ""
const PASSWORD = process.env.SWARM_PASSWORD ?? ""
const WORKSPACE = process.env.SWARM_WORKSPACE ?? "e2e"
const CASE = "console_case"

/** Replaces the open file's text in the code editor. */
async function retype(page: Page, text: string) {
  const editor = page.getByTestId("editor").locator(".cm-content")
  await editor.click()
  await page.keyboard.press("ControlOrMeta+A")
  await page.keyboard.press("Delete")
  await page.keyboard.insertText(text)
}

/** A page's link in the sidebar; breadcrumbs link to some of the same pages. */
function nav(page: Page, name: string) {
  return page.getByTestId("nav").getByRole("link", { name, exact: true })
}

test("create a case, run it, read the result and the replay, and fork it", async ({ page }) => {
  // No page before sign-in shows anything of the platform.
  await page.goto("/cases")
  await expect(page.getByLabel("Username")).toBeVisible()
  await page.getByLabel("Username").fill(USER)
  await page.getByLabel("Password").fill("not the password")
  await page.getByRole("button", { name: "Sign in" }).click()
  await expect(page.getByRole("alert")).toContainText("Sign-in failed")
  await page.getByLabel("Password").fill(PASSWORD)
  await page.getByRole("button", { name: "Sign in" }).click()
  await expect(page.getByTestId("whoami")).toHaveText(USER)

  // Create a case from the template.
  await page.getByRole("link", { name: "New case" }).click()
  await page.getByLabel("Workspace").fill(WORKSPACE)
  await page.getByLabel("Case id").fill(CASE)
  await page.getByRole("button", { name: "Start from a template" }).click()
  await expect(page.getByTestId("files").getByRole("button")).toHaveCount(5)
  await page.getByTestId("files").getByRole("button", { name: "task.md" }).click()
  await retype(page, "Write the word done to /workspace/out.txt, then stop.\n")
  await page.getByRole("button", { name: "Create case" }).click()
  await expect(page).toHaveURL(new RegExp(`/cases/${WORKSPACE}/${CASE}$`))
  await expect(page.getByRole("heading", { name: `${WORKSPACE}/${CASE}` })).toBeVisible()

  // A case that does not load is refused with the loader's message, and nothing is saved.
  await page.getByTestId("files").getByRole("button", { name: "case.yaml" }).click()
  await expect(page.getByTestId("current-file")).toHaveText("case.yaml")
  await retype(page, "schema_version: 1\nid: other_case\n")
  await page.getByRole("button", { name: "Save as revision 2" }).click()
  await expect(page.getByRole("alert")).toContainText("The case was not saved")
  await page.reload()

  // Edit a prompt and save revision 2.
  await page.getByTestId("files").getByRole("button", { name: "prompts/agent.md" }).click()
  await retype(page, "You are a careful engineer. <b>Say done</b> when the task is finished.\n")
  await page.getByLabel("Note").fill("clearer prompt")
  await page.getByRole("button", { name: "Save as revision 2" }).click()
  await expect(page.getByText("Saved as revision 2")).toBeVisible()
  await page.getByRole("tab", { name: "History" }).click()
  await expect(page.getByRole("row")).toHaveCount(3)
  await expect(page.getByRole("row").nth(1)).toContainText("clearer prompt")
  await expect(page.getByRole("row").nth(1)).toContainText(USER)
  await page.getByRole("tab", { name: "Diff" }).click()
  await expect(page.getByTestId("diff")).toHaveCount(1)
  await expect(page.getByTestId("diff")).toContainText("prompts/agent.md")
  await expect(page.getByTestId("diff")).toContainText("+You are a careful engineer. <b>Say done</b>")

  // Run revision 2, on a model chosen here: the case names none.
  await page.getByRole("tab", { name: "Run" }).click()
  await expect(page.getByRole("button", { name: "Run revision 2" })).toBeDisabled()
  await page.getByRole("group", { name: "Models for slot default" }).getByRole("checkbox", { name: "mock-model" }).click()
  await page.getByRole("button", { name: "Run revision 2" }).click()
  await expect(page).toHaveURL(/\/runs\?submission=/)
  await expect(page.getByTestId("submission")).toHaveCount(1)
  await expect(page.getByTestId("submission")).toContainText(`${WORKSPACE}/${CASE}@2`)
  await page.getByTestId("submission").getByTestId("run-link").first().click()
  await expect(page.locator("h1 [data-status]")).toHaveAttribute("data-status", "done", { timeout: 120_000 })

  // Scores, and the replay with a lane per agent.
  await expect(page.getByTestId("scores")).toContainText("wrote_output")
  await expect(page.getByTestId("lane").first()).toHaveText("agent")
  const model = page.locator('[data-testid="event"][data-type="model"]').first()
  await expect(model).toBeVisible()
  // Run content is text: the prompt's markup never becomes an element.
  await expect(page.getByTestId("replay").locator("b")).toHaveCount(0)
  await model.click()
  await expect(page.getByTestId("event-detail")).toContainText("Stored event")
  await expect(page.getByTestId("trace").getByRole("listitem").first()).toBeVisible()

  // Filter a type out and back in.
  const count = await page.locator('[data-testid="event"]').count()
  await page.getByRole("checkbox", { name: "model", exact: true }).click()
  await expect(page.locator('[data-testid="event"][data-type="model"]')).toHaveCount(0)
  await page.getByRole("checkbox", { name: "model", exact: true }).click()
  await expect(page.locator('[data-testid="event"]')).toHaveCount(count)

  // Fork from the model call on another model; the fork runs to its own result.
  const source = new URL(page.url()).pathname.split("/").pop() ?? ""
  await page.getByRole("button", { name: "Fork from here" }).click()
  await page.getByLabel("Model for slot default").selectOption("qwen3-8b")
  await page.getByRole("button", { name: "Fork", exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${source.replaceAll(".", "\\.")}\\.f1$`))
  await expect(page.getByText(`after seq`)).toBeVisible()
  await expect(page.locator("h1 [data-status]")).toHaveAttribute("data-status", "done", { timeout: 120_000 })
  const models = page.locator("dl > div").filter({ has: page.locator("dt", { hasText: /^Models$/ }) })
  await expect(models).toContainText("default")
  await expect(models).toContainText("qwen3-8b")

  // At phone width an event's detail covers the lanes with its controls on screen, and the
  // sidebar is a sheet that closes once a page is picked.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('[data-testid="event"]').first().click()
  await expect(page.getByTestId("event-detail").getByRole("button", { name: "Close" })).toBeInViewport({ ratio: 1 })
  await page.getByTestId("event-detail").getByRole("button", { name: "Close" }).click()
  await page.getByRole("button", { name: "Toggle Sidebar" }).click()
  await page.getByRole("dialog").getByRole("link", { name: "Runs", exact: true }).click()
  await expect(page).toHaveURL(/\/runs$/)
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.setViewportSize({ width: 1280, height: 720 })

  // The submission's rates, from the analysis service.
  await nav(page, "Runs").click()
  // The workspace filter lists the library's workspaces and is applied by the server.
  await page.getByLabel("Workspace").selectOption(WORKSPACE)
  await expect(page).toHaveURL(new RegExp(`workspace=${WORKSPACE}`))
  await expect(page.getByTestId("submission").first()).toContainText(`${WORKSPACE}/${CASE}`)
  await page.getByLabel("Case").fill(CASE)
  await page.getByLabel("Case").blur()
  await page.getByTestId("submission").first().getByRole("button", { name: "Show rates" }).click()
  await expect(page.getByTestId("rates")).toContainText("wrote_output")

  // Analysis: a query over the exported runs.
  await nav(page, "Analysis").click()
  await page.getByLabel("One SELECT").fill(`SELECT run_id, status FROM runs WHERE case_id = '${CASE}' AND forked_from IS NULL`)
  await page.getByRole("button", { name: "Run query" }).click()
  await expect(page.getByTestId("query-result")).toContainText(source)
  await page.getByLabel("One SELECT").fill("SELECT * FROM read_csv('/etc/passwd')")
  await page.getByRole("button", { name: "Run query" }).click()
  await expect(page.getByRole("alert")).toContainText("The query did not run")

  // Signing out ends the session for the next page load too.
  await page.getByTestId("whoami").click()
  await page.getByRole("menuitem", { name: "Sign out" }).click()
  await expect(page.getByLabel("Username")).toBeVisible()
  await page.goto("/runs")
  await expect(page.getByLabel("Username")).toBeVisible()

  // A session that ends while a page is open (here: its cookie is gone) puts the sign-in form
  // back at the next call, with no reload.
  await page.getByLabel("Username").fill(USER)
  await page.getByLabel("Password").fill(PASSWORD)
  await page.getByRole("button", { name: "Sign in" }).click()
  await expect(page.getByTestId("whoami")).toHaveText(USER)
  await page.context().clearCookies()
  await nav(page, "Cases").click()
  await expect(page.getByLabel("Username")).toBeVisible()
  await expect(page.getByTestId("whoami")).toHaveCount(0)
})

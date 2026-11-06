import { expect, test } from "@playwright/test"

test.use({
  launchOptions: {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined,
    args: ["--enable-unsafe-swiftshader"],
  },
})

test("the character scene responds to the pointer and can stay still", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 820 })
  await page.emulateMedia({ reducedMotion: "no-preference" })
  await page.goto("/")
  await expect(page.getByRole("heading", { name: "DUNE" })).toBeVisible()
  await expect(page.locator(".dune-home")).toHaveAttribute("data-ascii", "interactive")

  const clip = { x: 220, y: 150, width: 380, height: 240 }
  const snapshot = () => page.screenshot({ clip })
  const still = await snapshot()
  await page.mouse.move(410, 270)
  await expect.poll(async () => (await snapshot()).equals(still)).toBe(false)

  await page.getByRole("button", { name: "Pause effects" }).click()
  await page.mouse.move(410, 270)
  await expect.poll(async () => (await snapshot()).equals(still)).toBe(true)
  await page.getByRole("button", { name: "Resume effects" }).click()

  await page.emulateMedia({ reducedMotion: "reduce" })
  await expect(page.getByRole("button", { name: "Pause effects" })).toBeHidden()
  await expect(page.locator(".dune-ascii").first()).toBeVisible()
  await expect(page.getByRole("button", { name: "Sign in" })).toBeEnabled()
})

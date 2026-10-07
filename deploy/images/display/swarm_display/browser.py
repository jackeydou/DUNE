"""`browser`: Chromium on the screen, driven through Playwright.

Every action answers with the active tab's URL, title, and an accessibility snapshot in which
each element carries a `[ref=eN]`; actions on an element name that ref. The window is the same
one `computer` sees, so the two tools can be mixed.
"""

import asyncio
from typing import Any

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

from swarm_display import SCREENSHOT, STATE, ActionError

SNAPSHOT_CHARS = 40_000
ACTION_TIMEOUT_MS = 10_000
NAVIGATION_TIMEOUT_MS = 30_000
SCROLL_STEP_PX = 400


class Browser:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self.active = context.pages[0]
        context.on("page", self._opened)
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    @classmethod
    async def launch(cls, width: int, height: int, url: str) -> "Browser":
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=STATE / "profile",
            headless=False,
            no_viewport=True,
            # gVisor or runc is the sandbox; Chromium's own needs namespaces a sandbox lacks.
            chromium_sandbox=False,
            args=[
                "--window-position=0,0",
                f"--window-size={width},{height}",
                "--start-maximized",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--disable-breakpad",
            ],
        )
        browser = cls(context)
        if url != "about:blank":
            await browser.active.goto(url, wait_until="domcontentloaded")
        return browser

    def _opened(self, page: Page) -> None:
        self.active = page

    async def act(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        page = self.active
        match action:
            case "navigate":
                await page.goto(_text(args, "url"), wait_until="domcontentloaded")
            case "back":
                await page.go_back(wait_until="domcontentloaded")
            case "forward":
                await page.go_forward(wait_until="domcontentloaded")
            case "reload":
                await page.reload(wait_until="domcontentloaded")
            case "snapshot" | "screenshot":
                pass
            case "click":
                await _ref(page, args).click()
            case "hover":
                await _ref(page, args).hover()
            case "type":
                target = _ref(page, args)
                await target.fill(_text(args, "text"))
                if args.get("submit"):
                    await target.press("Enter")
            case "select":
                values = args.get("values")
                if not isinstance(values, list):
                    raise ActionError("`values` must be a list of option values or labels")
                await _ref(page, args).select_option([str(v) for v in values])
            case "press":
                await page.keyboard.press(_text(args, "key"))
            case "scroll":
                steps = int(args.get("amount", 3)) * SCROLL_STEP_PX
                dx, dy = {"up": (0, -steps), "down": (0, steps), "left": (-steps, 0)}.get(
                    args.get("direction", "down"), (steps, 0)
                )
                await page.mouse.wheel(dx, dy)
            case "wait":
                await asyncio.sleep(float(args.get("seconds", 1)))
            case "tabs":
                pass
            case "tab_new":
                page = await self.context.new_page()
                self.active = page
                if args.get("url"):
                    await page.goto(_text(args, "url"), wait_until="domcontentloaded")
            case "tab_select":
                self.active = self._tab(args)
                await self.active.bring_to_front()
            case "tab_close":
                closing = self._tab(args) if "index" in args else page
                if len(self.context.pages) == 1:
                    raise ActionError("this is the last tab; open another before closing it")
                await closing.close()
                self.active = self.context.pages[-1]
                await self.active.bring_to_front()
            case _:
                raise ActionError(f"unknown browser action {action!r}")
        if action == "screenshot" or args.get("screenshot"):
            await self.active.screenshot(path=SCREENSHOT, type="png")
        return await self._describe()

    def _tab(self, args: dict[str, Any]) -> Page:
        index = args.get("index")
        pages = self.context.pages
        if not isinstance(index, int) or not 1 <= index <= len(pages):
            raise ActionError(f"`index` must be a tab number from 1 to {len(pages)}")
        return pages[index - 1]

    async def _describe(self) -> str:
        page = self.active
        pages = self.context.pages
        lines = [f"Tab {pages.index(page) + 1} of {len(pages)}"]
        if len(pages) > 1:
            for i, p in enumerate(pages, 1):
                lines.append(f"  {i}. {await p.title()} - {p.url}")
        lines += [f"URL: {page.url}", f"Title: {await page.title()}", ""]
        snapshot = await page.aria_snapshot(mode="ai")
        if len(snapshot) > SNAPSHOT_CHARS:
            snapshot = (
                snapshot[:SNAPSHOT_CHARS]
                + f"\n[snapshot cut at {SNAPSHOT_CHARS} of {len(snapshot)} characters; "
                "scroll, or act on what is shown]"
            )
        lines.append(snapshot)
        return "\n".join(lines) + "\n"


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ActionError(f"`{name}` is required")
    return value


def _ref(page: Page, args: dict[str, Any]) -> Locator:
    """The element a snapshot named. Playwright 1.64's `page.get_by_ref` replaces the selector."""
    ref = _text(args, "ref")
    if not ref.isalnum():
        raise ActionError(f"`ref` {ref!r} is not a ref from the snapshot, such as `e12`")
    return page.locator(f"aria-ref={ref}")

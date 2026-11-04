"""Private original frontend checks, run only in a fresh offline grader container."""

import base64
import hashlib
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]


def main() -> None:
    scenario = json.loads(Path("/grader/frontend-case.json").read_text())["scenario"]
    html = Path("/workspace/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = f"http://127.0.0.1:{server.server_port}/"
    checks: list[dict[str, Any]] = []
    images: list[dict[str, str]] = []

    def check(name: str, action: Callable[[], None]) -> None:
        try:
            action()
            checks.append(
                {"name": name, "passed": True, "detail": "Observed in the isolated browser"}
            )
        except Exception as exc:
            checks.append({"name": name, "passed": False, "detail": str(exc)[:600]})

    def require(value: object, message: str) -> None:
        if not value:
            raise AssertionError(message)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, reduced_motion="reduce"
        )
        context.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(address) else route.abort()
            ),
        )
        page = context.new_page()
        page.set_default_timeout(1200)
        page.goto(address, wait_until="domcontentloaded")
        check(
            "document_language",
            lambda: require(
                bool(page.locator("html").get_attribute("lang")), "Declare the document language"
            ),
        )
        check(
            "document_title",
            lambda: require(bool(page.title().strip()), "Provide a meaningful page title"),
        )
        check(
            "main_landmark",
            lambda: require(page.get_by_role("main").count() == 1, "Use one main landmark"),
        )
        check(
            "viewport_metadata",
            lambda: require(
                page.locator('meta[name="viewport"]').count() == 1,
                "Declare mobile viewport metadata",
            ),
        )

        def no_overflow() -> None:
            require(
                page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                "Horizontal page overflow",
            )

        check("desktop_no_overflow", no_overflow)

        def dashboard() -> None:
            search = page.get_by_role("searchbox", name="Search orders")
            status = page.get_by_label("Status filter")
            table = page.get_by_role("table")
            require(table.locator("tbody tr").count() == 5, "Initially show five orders")
            require("405" in page.locator("output").inner_text(), "Initial sum must be $405")
            status.select_option(label="Paid")
            require(table.locator("tbody tr").count() == 3, "Paid filter must show three orders")
            require("225" in page.locator("output").inner_text(), "Paid sum must be $225")
            search.fill("ELI")
            require(
                table.locator("tbody tr").count() == 1 and "Eli" in table.inner_text(),
                "Search is case insensitive and combines with status",
            )
            status.select_option(label="New")
            require(
                table.locator("tbody tr").count() == 0, "Combined nonmatching filter must be empty"
            )
            require("$0" in page.locator("output").inner_text(), "Empty result total is zero")
            search.fill("")
            status.select_option(label="All")

        def form_invalid() -> None:
            page.get_by_label("Full name", exact=True).fill("Alex Rivera")
            page.get_by_label("Email", exact=True).fill("broken-address")
            page.get_by_role("button", name="Send request").click()
            require(
                "email" in page.get_by_role("alert").inner_text().lower(),
                "Invalid email must have accessible explanatory alert",
            )
            require(
                "Request received" not in page.get_by_role("status").inner_text(),
                "Invalid request must not succeed",
            )

        def form_valid() -> None:
            page.get_by_label("Full name", exact=True).fill("Alex Rivera")
            page.get_by_label("Email", exact=True).fill("alex@example.test")
            page.get_by_label("Contact reason").select_option(label="Invoice")
            page.get_by_role("button", name="Send request").focus()
            page.keyboard.press("Enter")
            require(
                "Request received for Alex Rivera" in page.get_by_role("status").inner_text(),
                "Successful keyboard submission must be announced",
            )
            require(
                page.get_by_label("Email", exact=True).input_value() == "alex@example.test",
                "Entered values must be preserved",
            )

        def tabs() -> None:
            analyst = page.get_by_role("tab", name="Analyst", exact=True)
            operator = page.get_by_role("tab", name="Operator", exact=True)
            settings = page.get_by_role("tab", name="Settings", exact=True)
            require(
                analyst.get_attribute("aria-selected") == "true",
                "Analyst must be initially selected",
            )
            analyst.focus()
            page.keyboard.press("ArrowRight")
            require(
                operator.get_attribute("aria-selected") == "true"
                and operator.evaluate("e=>e===document.activeElement"),
                "ArrowRight must select and focus Operator",
            )
            page.keyboard.press("End")
            require(settings.get_attribute("aria-selected") == "true", "End must select Settings")
            page.keyboard.press("Home")
            require(analyst.get_attribute("aria-selected") == "true", "Home must select Analyst")
            page.keyboard.press("ArrowLeft")
            require(
                settings.get_attribute("aria-selected") == "true", "ArrowLeft must wrap to Settings"
            )
            require(
                page.locator('[role=tab][tabindex="0"]').count() == 1,
                "Roving tab order must have one entry",
            )
            require(page.get_by_role("tabpanel").count() == 1, "Only selected panel is exposed")
            panel = page.get_by_role("tabpanel")
            require(
                panel.get_attribute("id") == settings.get_attribute("aria-controls"),
                "Selected tab must control the active panel",
            )
            require(
                panel.get_attribute("aria-labelledby") == settings.get_attribute("id"),
                "Panel must be labelled by its tab",
            )

        def dialog_dismiss() -> None:
            opener = page.get_by_role("button", name="Delete project", exact=True)
            opener.click()
            dialog = page.get_by_role("dialog", name="Delete Atlas project?", exact=True)
            require(dialog.get_attribute("aria-modal") == "true", "Modal must report aria-modal")
            cancel = dialog.get_by_role("button", name="Cancel", exact=True)
            confirm = dialog.get_by_role("button", name="Confirm delete", exact=True)
            require(
                cancel.evaluate("e=>e===document.activeElement"),
                "Cancel must receive initial focus",
            )
            page.keyboard.press("Shift+Tab")
            require(
                confirm.evaluate("e=>e===document.activeElement"),
                "Shift+Tab must wrap inside modal",
            )
            page.keyboard.press("Tab")
            require(cancel.evaluate("e=>e===document.activeElement"), "Tab must wrap inside modal")
            page.keyboard.press("Escape")
            require(
                dialog.count() == 0 and opener.evaluate("e=>e===document.activeElement"),
                "Escape must dismiss and restore focus",
            )

        def dialog_confirm() -> None:
            page.get_by_role("button", name="Delete project", exact=True).click()
            page.get_by_role("button", name="Confirm delete", exact=True).click()
            require(
                "Project deleted" in page.get_by_role("status").inner_text(),
                "Confirm must announce completion",
            )
            require(page.get_by_role("dialog").count() == 0, "Confirmation must close modal")

        def desktop_navigation() -> None:
            nav = page.get_by_role("navigation", name="Primary navigation")
            require(
                nav.get_by_role("link", name="Pricing", exact=True).is_visible(),
                "Desktop links must be visible",
            )

        def mobile_navigation() -> None:
            page.set_viewport_size({"width": 390, "height": 844})
            menu = page.get_by_role("button", name="Menu", exact=True)
            require(menu.get_attribute("aria-expanded") == "false", "Menu starts collapsed")
            menu.click()
            require(
                menu.get_attribute("aria-expanded") == "true", "Menu must report expanded state"
            )
            require(
                bool(menu.get_attribute("aria-controls")),
                "Menu must identify controlled navigation",
            )
            page.get_by_role("link", name="Pricing", exact=True).focus()
            page.keyboard.press("Enter")
            require(
                page.get_by_role("heading", name="Pricing", exact=True).is_visible(),
                "Keyboard navigation must show Pricing",
            )
            menu.click()
            page.keyboard.press("Escape")
            require(
                menu.get_attribute("aria-expanded") == "false"
                and menu.evaluate("e=>e===document.activeElement"),
                "Escape must collapse and restore focus",
            )

        def theme() -> None:
            require(
                "Light mode" in page.get_by_role("status").inner_text(),
                "Default must be Light mode",
            )
            before = page.locator("body").evaluate(
                "e=>({bg:getComputedStyle(e).backgroundColor,fg:getComputedStyle(e).color})"
            )
            page.get_by_role("button", name="Switch to dark mode", exact=True).click()
            require(
                "Dark mode" in page.get_by_role("status").inner_text(),
                "Toggle must announce Dark mode",
            )
            after = page.locator("body").evaluate(
                "e=>({bg:getComputedStyle(e).backgroundColor,fg:getComputedStyle(e).color})"
            )
            require(
                before["bg"] != after["bg"]
                and before["fg"] != after["fg"]
                and after["bg"] != after["fg"],
                "Both foreground and background must visibly change",
            )
            page.reload(wait_until="domcontentloaded")
            require(
                "Dark mode" in page.get_by_role("status").inner_text(), "Theme must survive reload"
            )
            require(
                page.get_by_role("button", name="Switch to light mode", exact=True).is_visible(),
                "Toggle must name its next action",
            )

        actions: dict[str, list[tuple[str, Callable[[], None]]]] = {
            "orders-dashboard": [("combined_search_filter_and_totals", dashboard)],
            "support-form": [
                ("invalid_email_feedback", form_invalid),
                ("valid_keyboard_submission", form_valid),
            ],
            "keyboard-tabs": [("tab_keyboard_and_semantics", tabs)],
            "confirmation-dialog": [
                ("modal_focus_and_dismiss", dialog_dismiss),
                ("modal_confirmation", dialog_confirm),
            ],
            "responsive-navigation": [
                ("desktop_navigation", desktop_navigation),
                ("mobile_navigation", mobile_navigation),
            ],
            "persistent-theme": [("theme_toggle_and_persistence", theme)],
        }
        for name, action in actions[scenario]:
            check(name, action)
        for viewport in [{"width": 1280, "height": 900}, {"width": 390, "height": 844}]:
            page.set_viewport_size(viewport)
            check(f"overflow_at_{viewport['width']}", no_overflow)
            raw = page.screenshot(full_page=True, animations="disabled", timeout=5000)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Frontend screenshot exceeded two MiB")
            images.append(
                {
                    "name": f"frontend-{viewport['width']}.png",
                    "media_type": "image/png",
                    "base64": base64.b64encode(raw).decode(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        context.close()
        browser.close()
    server.shutdown()
    server.server_close()
    result = {
        "passed": all(c["passed"] for c in checks),
        "score": sum(c["passed"] for c in checks) / len(checks),
        "checks": checks,
        "artifacts": images,
        "evidence": {
            "track": "benchmaxxing-original-frontend/1",
            "scenario": scenario,
            "network": "none",
            "viewports": [1280, 390],
            "functional_grade": "objective browser observations",
            "aesthetic_review": {
                "status": "needs_review",
                "score": None,
                "rubric": [
                    "Visual hierarchy",
                    "Spacing and typography",
                    "Coherent component design",
                    "Responsive composition",
                ],
            },
            "accessibility_scope": (
                "Specified semantics and keyboard behaviors only; not a comprehensive WCAG audit"
            ),
        },
    }
    Path("/workspace/frontend-result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()

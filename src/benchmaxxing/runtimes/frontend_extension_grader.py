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

        def cart() -> None:
            quantity = page.get_by_role("spinbutton", name="Quantity")
            delivery = page.get_by_label("Delivery")
            total = page.locator("output")
            require(total.inner_text().strip() == "$16", "Initial total is $16")
            quantity.fill("3")
            require(total.inner_text().strip() == "$40", "Quantity updates total")
            delivery.select_option(label="Pickup")
            require(total.inner_text().strip() == "$36", "Pickup removes delivery cost")
            for invalid in ["0", ""]:
                quantity.fill(invalid)
                page.get_by_role("button", name="Update cart", exact=True).click()
                require(
                    "quantity" in page.get_by_role("alert").inner_text().lower(),
                    "Invalid quantity requires explanatory alert",
                )
                require(total.inner_text().strip() == "$36", "Invalid input retains total")
            quantity.fill("2")
            button = page.get_by_role("button", name="Update cart", exact=True)
            button.focus()
            page.keyboard.press("Enter")
            require(total.inner_text().strip() == "$24", "Valid quantity recalculates")
            require(not page.get_by_role("alert").inner_text().strip(), "Clear old error")
            require(
                page.get_by_role("status").filter(has_text="Cart updated").count() > 0,
                "Announce successful update",
            )

        def accordion() -> None:
            buttons = [
                page.get_by_role("button", name=name, exact=True)
                for name in ["Shipping", "Returns", "Warranty"]
            ]
            ids = [button.get_attribute("aria-controls") for button in buttons]
            require(all(ids) and len(set(ids)) == 3, "Unique controlled answer regions")
            for button, target in zip(buttons, ids, strict=True):
                require(
                    button.get_attribute("aria-expanded") == "false",
                    "All answers initially collapsed",
                )
                require(
                    not page.locator(f'[id="{target}"]').is_visible(),
                    "Collapsed answer must be hidden",
                )
            for index, answer in [
                (0, "Orders ship in two days"),
                (1, "Returns accepted within 30 days"),
                (1, None),
                (2, "Coverage lasts one year"),
            ]:
                button = buttons[index]
                button.focus()
                page.keyboard.press("Space" if index == 1 else "Enter")
                require(button.evaluate("e=>e===document.activeElement"), "Retain focus")
                for other, target in zip(buttons, ids, strict=True):
                    expanded = other == button and answer is not None
                    require(
                        other.get_attribute("aria-expanded") == str(expanded).lower(),
                        "Only activated answer may be expanded",
                    )
                    region = page.locator(f'[id="{target}"]')
                    require(region.is_visible() == expanded, "Synchronize answer visibility")
                if answer:
                    require(page.get_by_text(answer, exact=True).is_visible(), "Reveal answer")

        def pagination() -> None:
            previous = page.get_by_role("button", name="Previous", exact=True)
            next_button = page.get_by_role("button", name="Next", exact=True)

            def state(number: int, titles: list[str]) -> None:
                listing = page.get_by_role("list", name="Reading list", exact=True)
                require(
                    listing.get_by_role("listitem").all_inner_texts() == titles,
                    "Show exact entries in order",
                )
                require(
                    page.get_by_role("status").inner_text().strip() == f"Page {number} of 3",
                    "Announce current page",
                )
                require(previous.is_disabled() == (number == 1), "Previous boundary state")
                require(next_button.is_disabled() == (number == 3), "Next boundary state")

            state(1, ["Aurora", "Beacon"])
            next_button.focus()
            page.keyboard.press("Enter")
            state(2, ["Cedar", "Delta"])
            next_button.click()
            state(3, ["Ember"])
            previous.focus()
            page.keyboard.press("Space")
            state(2, ["Cedar", "Delta"])
            previous.click()
            state(1, ["Aurora", "Beacon"])

        actions: dict[str, list[tuple[str, Callable[[], None]]]] = {
            "quantity-cart": [("quantity_delivery_validation", cart)],
            "shipping-accordion": [("accordion_keyboard_and_state", accordion)],
            "reading-pagination": [("pagination_content_and_boundaries", pagination)],
        }
        # Every extension interaction is repeated from a clean document at each size.
        for width, height in [(1280, 900), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            page.reload(wait_until="domcontentloaded")
            for name, action in actions[scenario]:
                check(f"{name}_at_{width}", action)
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
            "track": "benchmaxxing-original-frontend-extended/1",
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

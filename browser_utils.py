"""Общие браузерные хелперы для register_batch.py и register_magic_link.py."""

from __future__ import annotations

import time
from typing import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

VISIBLE_TEXT_JS = """() => {
    const walk = (node) => {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
        if (node.nodeType !== Node.ELEMENT_NODE) return "";
        const style = window.getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
            return "";
        }
        const tag = node.tagName.toLowerCase();
        if (["script", "style", "noscript"].includes(tag)) return "";
        return Array.from(node.childNodes).map(walk).join(" ");
    };
    return walk(document.body).replace(/\\s+/g, " ").trim();
}"""

CAPTCHA_STATE_JS = """() => {
    const text = (document.body.innerText || "").toLowerCase();
    const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    const widgets = document.querySelectorAll(
        '[id^="cf-chl"], .cf-turnstile, iframe[src*="turnstile"]'
    );
    const tokenInputs = [...document.querySelectorAll(
        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    )];
    const tokens = tokenInputs.map((el) => el.value || "").filter(Boolean);
    return {
        hasTurnstileIframe: Boolean(iframe),
        widgetCount: widgets.length,
        turnstileApi: typeof window.turnstile !== "undefined",
        hasToken: tokens.some((t) => t.length > 20),
        tokenLength: tokens[0] ? tokens[0].length : 0,
        failed: text.includes("verification failed"),
        success: /\\b(success|успешно)\\b/.test(text),
        sent: text.includes("check your email")
            || text.includes("sign-in link has been sent")
            || text.includes("we sent"),
    };
}"""


def visible_text(page) -> str:
    try:
        return page.evaluate(VISIBLE_TEXT_JS) or ""
    except PlaywrightError:
        return ""


def captcha_state(page) -> dict:
    try:
        return page.evaluate(CAPTCHA_STATE_JS) or {}
    except PlaywrightError:
        return {}


def goto_retry(
    page,
    url: str,
    *,
    tries: int = 3,
    timeout: int = 45_000,
    wait_until: str = "domcontentloaded",
    log: Callable[[str], None] | None = None,
):
    """page.goto с повторами: сеть моргнула — не повод дёргать человека."""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            last = exc
            if attempt >= tries:
                break
            pause = 2 * attempt
            if log:
                log(f"goto не удался ({attempt}/{tries}): {str(exc).splitlines()[0][:120]} — через {pause}с")
            time.sleep(pause)
    assert last is not None
    raise last

#!/usr/bin/env python3
"""SmailPro Gmail через те же ручки, что сайт (без X-Api-Key).

Создание: GET /app/create (залогиненному Premium без капчи; иначе x-captcha).
Письма:   POST /app/inbox → GET api.sonjj.com/v1/temp_gmail/inbox?payload=
Тело:     GET /app/message → GET api.sonjj.com/v1/temp_gmail/message?payload=
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, Callable

ORIGIN = "https://smailpro.com"
API = "https://api.sonjj.com/v1/temp_gmail"
DEFAULT_BOOK = Path("smailpro_book.json")
DEFAULT_COOKIES = Path("smailpro_cookies.json")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Box:
    address: str
    timestamp: int
    key: str
    kind: str = "real"
    server: str = "2"
    has_account: bool = False
    account_status: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "timestamp": self.timestamp,
            "key": self.key,
            "kind": self.kind,
            "server": self.server,
            "has_account": self.has_account,
            "account_status": self.account_status,
        }


class InboxError(RuntimeError):
    pass


def load_cookies(path: Path = DEFAULT_COOKIES) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        items = [(str(k), str(v)) for k, v in raw.items() if v]
    elif isinstance(raw, list):
        for rec in raw:
            if not isinstance(rec, dict) or not rec.get("name") or rec.get("value") is None:
                continue
            domain = str(rec.get("domain") or "")
            if domain and "smailpro" not in domain and "sonjj" not in domain:
                continue
            items.append((str(rec["name"]), str(rec["value"])))
    out: dict[str, str] = {}
    for key, val in items:
        out[key] = urllib.parse.unquote(val)
    return out


def save_cookies(jar: dict[str, str], path: Path = DEFAULT_COOKIES) -> None:
    clean = {k: v for k, v in jar.items() if k and v}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _absorb_set_cookie(headers: HTTPMessage | None, path: Path = DEFAULT_COOKIES) -> int:
    """Laravel крутит sonjj_session — без этого файл кук через день мёртвый."""
    if headers is None:
        return 0
    get_all = getattr(headers, "get_all", None)
    raw_items = get_all("Set-Cookie") if callable(get_all) else None
    if not raw_items:
        one = headers.get("Set-Cookie")
        raw_items = [one] if one else []
    jar = load_cookies(path)
    added = 0
    for item in raw_items:
        first = str(item).split(";", 1)[0]
        if "=" not in first:
            continue
        name, val = first.split("=", 1)
        name, val = name.strip(), urllib.parse.unquote(val.strip())
        if not name or val in ("deleted",):
            continue
        if jar.get(name) != val:
            jar[name] = val
            added += 1
    if added:
        save_cookies(jar, path)
    return added


def _auth_headers(cookies: dict[str, str] | None = None) -> dict[str, str]:
    jar = cookies if cookies is not None else load_cookies()
    if not jar:
        return {}
    hdrs = {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())}
    xsrf = jar.get("XSRF-TOKEN")
    if xsrf:
        hdrs["X-XSRF-TOKEN"] = urllib.parse.unquote(xsrf)
    return hdrs


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[int, str]:
    hdrs = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": f"{ORIGIN}/temporary-email",
    }
    hdrs.update(_auth_headers())
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _absorb_set_cookie(resp.headers)
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        _absorb_set_cookie(exc.headers)
        return exc.code, exc.read().decode("utf-8", "replace")


def touch_session() -> bool:
    """Пинаем сайт, чтобы Laravel выдал свежий Set-Cookie в файл."""
    if not load_cookies():
        return False
    status, _raw = _request(f"{ORIGIN}/temporary-email", timeout=20)
    return 200 <= status < 400


def refresh(box: Box) -> str:
    """Новый JWT для чтения inbox. Капча не нужна."""
    body = json.dumps(
        [{"address": box.address, "timestamp": int(box.timestamp), "key": box.key}]
    ).encode()
    status, raw = _request(
        f"{ORIGIN}/app/inbox",
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    if status != 200:
        raise InboxError(f"app/inbox HTTP {status}: {raw[:240]}")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboxError(f"app/inbox не JSON: {raw[:240]}") from exc
    if not items:
        raise InboxError("app/inbox пустой ответ")
    item = items[0]
    if item.get("key"):
        box.key = str(item["key"])
    payload = item.get("payload")
    if not payload:
        raise InboxError(f"app/inbox без payload: {raw[:240]}")
    return str(payload)


def fetch_inbox(box: Box) -> list[dict[str, Any]]:
    payload = refresh(box)
    url = f"{API}/inbox?payload={urllib.parse.quote(payload)}"
    status, raw = _request(url, headers={"Referer": f"{ORIGIN}/"})
    if status != 200:
        raise InboxError(f"temp_gmail/inbox HTTP {status}: {raw[:240]}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboxError(f"temp_gmail/inbox не JSON: {raw[:240]}") from exc
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        raise InboxError(f"странный inbox: {raw[:240]}")
    return messages


def fetch_message(box: Box, mid: str) -> dict[str, Any]:
    qs = urllib.parse.urlencode({"email": box.address, "mid": mid})
    status, raw = _request(f"{ORIGIN}/app/message?{qs}")
    if status != 200 or not raw.strip():
        raise InboxError(f"app/message HTTP {status}: {raw[:240]}")
    payload = raw.strip().strip('"')
    url = f"{API}/message?payload={urllib.parse.quote(payload)}"
    status, raw = _request(url, headers={"Referer": f"{ORIGIN}/"})
    if status != 200:
        raise InboxError(f"temp_gmail/message HTTP {status}: {raw[:240]}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboxError(f"temp_gmail/message не JSON: {raw[:240]}") from exc
    return data if isinstance(data, dict) else {"raw": data}


def message_blob(msg: dict[str, Any]) -> str:
    parts = [str(msg.get(k) or "") for k in msg]
    return "\n".join(parts)


class Book:
    def __init__(self, path: Path = DEFAULT_BOOK) -> None:
        self.path = path
        self.data: dict[str, Any] = {"updated_at": None, "mailboxes": {}}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("mailboxes"), dict):
                self.data = raw

    def _boxes(self) -> dict[str, dict[str, Any]]:
        boxes = self.data.setdefault("mailboxes", {})
        if not isinstance(boxes, dict):
            raise SystemExit(f"{self.path}: mailboxes должен быть объектом")
        return boxes

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, box: Box) -> None:
        rec = self._boxes().get(box.address.lower()) or {}
        rec.update(box.as_dict())
        rec.setdefault("first_seen", now_iso())
        rec["last_seen"] = now_iso()
        self._boxes()[box.address.lower()] = rec
        self.save()

    def get(self, email: str) -> Box | None:
        rec = self._boxes().get(email.lower())
        if not isinstance(rec, dict) or not rec.get("key"):
            return None
        return Box(
            address=str(rec["address"]),
            timestamp=int(rec["timestamp"]),
            key=str(rec["key"]),
            kind=str(rec.get("kind") or "real"),
            server=str(rec.get("server") or "2"),
            has_account=bool(rec.get("has_account")),
            account_status=str(rec.get("account_status") or "none"),
        )

    def unused(self) -> list[Box]:
        out: list[Box] = []
        for rec in self._boxes().values():
            if not isinstance(rec, dict) or rec.get("has_account"):
                continue
            if (rec.get("account_status") or "none") != "none":
                continue
            if not rec.get("key"):
                continue
            box = self.get(str(rec["address"]))
            if box:
                out.append(box)
        return out

    def mark(self, email: str, status: str, *, has_account: bool = True) -> None:
        rec = self._boxes().get(email.lower())
        if not rec:
            return
        rec["has_account"] = has_account
        rec["account_status"] = status
        rec["account_marked_at"] = now_iso()
        self.save()


def box_from_create(data: dict[str, Any], *, kind: str, server: str) -> Box:
    addr = str(data.get("address") or "")
    if not addr or not data.get("key"):
        raise InboxError(f"create без address/key: {data!r}"[:240])
    return Box(
        address=addr,
        timestamp=int(data["timestamp"]),
        key=str(data["key"]),
        kind=kind,
        server=server,
    )


def create(*, kind: str = "real", server: str = "2", captcha: str | None = None) -> Box:
    """Создать Gmail HTTP-ом. Нужны куки залогиненного Premium в smailpro_cookies.json."""
    qs = urllib.parse.urlencode(
        {
            "username": "random",
            "type": kind,
            "domain": "gmail.com",
            "server": str(server),
        }
    )
    extra = {}
    if captcha:
        extra["x-captcha"] = captcha
    status, raw = _request(f"{ORIGIN}/app/create?{qs}", headers=extra or None)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"raw": raw}
    if not isinstance(data, dict) or not data.get("address"):
        raise InboxError(f"create HTTP {status}: {raw[:240]}")
    return box_from_create(data, kind=kind, server=str(server))


def looks_like_captcha(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return "captcha" in text or "403" in text


def inject_cookies(context) -> int:
    jar = load_cookies()
    if not jar:
        return 0
    payload = [
        {"name": name, "value": value, "url": f"{ORIGIN}/"}
        for name, value in jar.items()
    ]
    try:
        context.add_cookies(payload)
    except Exception:
        return 0
    return len(payload)


def harvest_cookies(context, path: Path = DEFAULT_COOKIES) -> int:
    jar = load_cookies(path)
    added = 0
    try:
        cookies = context.cookies()
    except Exception:
        return 0
    for rec in cookies:
        domain = str(rec.get("domain") or "")
        if "smailpro" not in domain and "sonjj" not in domain:
            continue
        name, val = str(rec.get("name") or ""), str(rec.get("value") or "")
        if not name or not val:
            continue
        if jar.get(name) != val:
            jar[name] = val
            added += 1
    if added:
        save_cookies(jar, path)
    return added


def _page_create(page, *, kind: str, server: str) -> dict[str, Any]:
    return page.evaluate(
        """async ({ kind, server }) => {
            const q = new URLSearchParams({
                username: 'random',
                type: kind,
                domain: 'gmail.com',
                server: String(server),
            });
            const tokenEl = document.querySelector(
                'textarea[name="cf-turnstile-response"], input[name="cf-turnstile-response"]'
            );
            let token = tokenEl && tokenEl.value ? tokenEl.value : '';
            try {
                if (!token && window.turnstile && window.turnstile.getResponse) {
                    token = window.turnstile.getResponse() || '';
                }
            } catch (e) {}
            const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
            if (token) headers['x-captcha'] = token;
            const r = await fetch('/app/create?' + q.toString(), {
                headers,
                credentials: 'include',
            });
            let body = null;
            try { body = await r.json(); } catch (e) { body = { raw: await r.text() }; }
            return { status: r.status, body, hasToken: Boolean(token && token.length > 20) };
        }""",
        {"kind": kind, "server": server},
    )


def create_from_page(
    page,
    *,
    kind: str = "real",
    server: str = "2",
    log: Callable[..., Any] = print,
    notify: Callable[[str, str], None] | None = None,
    click_turnstile: Callable[[], str] | None = None,
    auto_wait: int = 12,
    human_wait: int = 180,
) -> Box:
    """Создать ящик в уже открытом Chrome. Куки подставляет, после успеха пишет обратно.

    Сначала кликает Turnstile сам. Если не вышло — оставляет окно на SmailPro и зовёт человека.
    """
    context = page.context
    injected = inject_cookies(context)
    if injected:
        log(f"SmailPro: подставил {injected} кук в Chrome")
    page.goto(f"{ORIGIN}/temporary-email", wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(1200)
    harvest_cookies(context)

    deadline_auto = time.time() + max(3, auto_wait)
    while time.time() < deadline_auto:
        result = _page_create(page, kind=kind, server=server)
        status = int(result.get("status") or 0)
        body = result.get("body") or {}
        if body.get("address"):
            harvest_cookies(context)
            log(f"smailpro create HTTP {status} address={body.get('address')}")
            return box_from_create(body, kind=kind, server=server)
        if click_turnstile:
            how = click_turnstile()
            if how:
                log(f"SmailPro: кликнул капчу ({how})")
        page.wait_for_timeout(900)

    result = _page_create(page, kind=kind, server=server)
    body = result.get("body") or {}
    if body.get("address"):
        harvest_cookies(context)
        return box_from_create(body, kind=kind, server=str(server))

    msg = (
        "SmailPro просит капчу/логин в этом Chrome-окне. "
        "Пройди виджет или зайди в Premium — я заберу свежие куки сам."
    )
    log(msg)
    if notify:
        notify("SmailPro: нужна капча", "Открой инкогнито-окно, пройди проверку или залогинься.")
    print()
    print("  >>> SmailPro: в том же Chrome-окне пройди капчу или залогинься в Premium.")
    print("  >>> Когда ящик появится / галочка станет зелёной — продолжу сам.")
    print()

    deadline = time.time() + max(30, human_wait)
    last_status = int(result.get("status") or 0)
    last_body = body
    while time.time() < deadline:
        if click_turnstile:
            click_turnstile()
        page.wait_for_timeout(1500)
        try:
            result = _page_create(page, kind=kind, server=server)
        except Exception:
            continue
        last_status = int(result.get("status") or 0)
        last_body = result.get("body") or {}
        harvest_cookies(context)
        if last_body.get("address"):
            log(f"smailpro create HTTP {last_status} address={last_body.get('address')}")
            return box_from_create(last_body, kind=kind, server=str(server))
        left = int(deadline - time.time())
        if left and left % 15 < 2:
            log(f"SmailPro всё ещё капча/логин, жду ещё {left}с…")

    harvest_cookies(context)
    raise InboxError(f"create не дал ящик: HTTP {last_status} {last_body}")


def create_or_browser(
    page,
    *,
    kind: str = "real",
    server: str = "2",
    log: Callable[..., Any] = print,
    notify: Callable[[str, str], None] | None = None,
    click_turnstile: Callable[[], str] | None = None,
    auto_wait: int = 12,
    human_wait: int = 180,
) -> Box:
    """HTTP create; если сессия просит капчу — то же окно Chrome, потом сохранить куки."""
    try:
        touch_session()
        box = create(kind=kind, server=server)
        log(f"SmailPro HTTP create {box.address}")
        return box
    except InboxError as exc:
        if not looks_like_captcha(exc) and load_cookies():
            raise
        log(f"SmailPro HTTP не вышел ({exc}) — открываю smailpro.com в этом Chrome")
        return create_from_page(
            page,
            kind=kind,
            server=server,
            log=log,
            notify=notify,
            click_turnstile=click_turnstile,
            auto_wait=auto_wait,
            human_wait=human_wait,
        )


def wait_magic_link(
    box: Box,
    book: Book | None,
    seen_ids: set[str],
    extract_link,
    *,
    timeout: int,
    interval: float = 6,
    log=print,
) -> tuple[str, str | None]:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            messages = fetch_inbox(box)
            if book:
                book.upsert(box)
        except InboxError as exc:
            last_err = exc
            log(f"smailpro inbox: {exc} — подожду")
            time.sleep(interval)
            continue
        fresh = []
        for msg in messages:
            mid = str(msg.get("mid") or msg.get("id") or "")
            if mid and mid in seen_ids:
                continue
            fresh.append(msg)
        for msg in reversed(fresh):
            blob_msg = msg
            mid = str(msg.get("mid") or msg.get("id") or "")
            if mid and "nofx.one" not in message_blob(msg).lower():
                try:
                    blob_msg = fetch_message(box, mid)
                except InboxError as exc:
                    log(f"smailpro message {mid}: {exc}")
            link = extract_link([blob_msg, msg])
            if link:
                return link, mid or None
        left = int(deadline - time.time())
        log(f"smailpro: нового письма нет (ящик {len(messages)}, новых {len(fresh)}), ещё {left}с…")
        time.sleep(interval)
    extra = f" последняя ошибка: {last_err}" if last_err else ""
    raise InboxError(f"magic-link не пришёл за {timeout}с.{extra}")

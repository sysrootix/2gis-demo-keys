#!/usr/bin/env python3
"""Регистрация в 2ГИС Platform и создание демо-ключа.

Почта — SmailPro Real Gmail (отдельная книга от NOFX). Браузер — родной
Google Chrome в инкогнито через CDP. Ключи пишет в 2gis-keys.jsonl
(полная запись) и 2gis-demo-keys.json (массив UUID + аккаунты).

Запуск:
  python3 register_2gis.py --count 3
  python3 register_2gis.py          # спросит сколько
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

import smailpro as sm
from browser_utils import goto_retry, visible_text

PLATFORM = "https://platform.2gis.ru/ru"
DASHBOARD = f"{PLATFORM}/dashboard"
DEMO_CREATE = f"{PLATFORM}/keys/create/demo"
ID_HOST = "id.platform.2gis.ru"


def find_chrome() -> Path:
    for candidate in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ):
        if candidate.exists():
            return candidate
    raise SystemExit("не нашёл Chrome: установи Google Chrome или chromium")


def chrome_extra_args() -> list[str]:
    if sys.platform == "darwin":
        return []
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--password-store=basic",
    ]

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
KEY_URL_RE = re.compile(r"/ru/keys/(\d+)")
AUTO_RETRY_KINDS = {
    "dup",
    "already_exists",
    "no_mail",
    "mailbox_dead",
    "captcha",
    "send_failed",
    "no_form",
    "no_cabinet",
    "no_mailbox",
    "smailpro_captcha",
    "no_code",
    "no_company",
    "no_key",
}
MAX_AUTO_RETRIES = 3
AUTO_YES = False

FIRST_NAMES = ("Иван", "Пётр", "Алексей", "Дмитрий", "Сергей", "Анна", "Мария", "Ольга", "Елена", "Никита")
LAST_NAMES = ("Иванов", "Петров", "Сидоров", "Козлов", "Новиков", "Морозов", "Волков", "Соколов", "Лебедев")
PATRONYMICS = ("Иванович", "Петрович", "Сергеевич", "Алексеевич", "Дмитриевич", "Андреевич")


class NeedUser(RuntimeError):
    def __init__(self, msg: str, kind: str = "need_user", email: str | None = None) -> None:
        super().__init__(msg)
        self.kind = kind
        self.email = email


@dataclass
class Outcome:
    index: int
    email: str
    status: str
    seconds: float
    note: str = ""
    key: str | None = None


class Tee:
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for st in self.streams:
            try:
                st.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return bool(getattr(self.streams[0], "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.streams[0].fileno()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def setup_log_file(log_dir: Path | None) -> Path | None:
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{time.strftime('%Y-%m-%d')}-2gis.log"
    fh = path.open("a", encoding="utf-8", buffering=1)
    fh.write(f"\n===== 2gis запуск {now_iso()}  argv={' '.join(sys.argv[1:])}\n")
    sys.stdout = Tee(sys.stdout, fh)
    sys.stderr = Tee(sys.stderr, fh)
    return path


def desktop_notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\"",
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def ask(prompt: str, allowed: set[str] | None = None, default: str | None = None) -> str:
    if AUTO_YES:
        if default is None:
            raise SystemExit(f"--yes, но вопрос без ответа по умолчанию: {prompt.strip()}")
        log(f"{prompt.strip()} → {default} (--yes)")
        return default
    while True:
        raw = input(prompt).strip().lower()
        if allowed is None:
            return raw
        if raw in allowed:
            return raw
        print(f"  введи одно из: {', '.join(sorted(allowed))}")


def ask_count(cli_count: int | None) -> int:
    if cli_count is not None:
        if cli_count < 1:
            raise SystemExit("--count должен быть >= 1")
        return cli_count
    if AUTO_YES:
        raise SystemExit("с --yes нужен --count N")
    print()
    print("Скрипт: новая почта → регистрация 2ГИС → код из письма → компания → демо-ключ.")
    print()
    while True:
        raw = input("Сколько демо-ключей сделать? ").strip()
        if not raw.isdigit() or int(raw) < 1:
            print("  нужно целое число больше 0.")
            continue
        n = int(raw)
        if n > 5 and ask(f"  это {n} попыток (>5). Продолжить? [y/n] ", {"y", "n"}) != "y":
            continue
        return n


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("email"):
            out.append(rec)
    return out


def known_emails(*paths: Path) -> set[str]:
    seen: set[str] = set()
    for path in paths:
        for rec in load_jsonl(path):
            seen.add(str(rec["email"]).lower())
    return seen


def append_jsonl(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    key = str(rec.get("key") or "")
    shown = f"{key[:8]}…" if len(key) > 8 else key or "—"
    log(f"записал {rec.get('email')} ключ {shown} в {path}")


def rewrite_keys_json(json_path: Path, jsonl_path: Path) -> None:
    recs = load_jsonl(jsonl_path)
    keys = [str(r["key"]) for r in recs if r.get("key")]
    payload = {
        "updated_at": now_iso(),
        "count": len(keys),
        "keys": keys,
        "accounts": recs,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(json_path)
    try:
        os.chmod(json_path, 0o600)
    except OSError:
        pass


def gen_full_name() -> str:
    return f"{random.choice(LAST_NAMES)} {random.choice(FIRST_NAMES)} {random.choice(PATRONYMICS)}"


def gen_phone() -> str:
    return "+79" + "".join(secrets.choice(string.digits) for _ in range(9))


def gen_password() -> str:
    chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    pool = string.ascii_letters + string.digits
    chars += [secrets.choice(pool) for _ in range(9)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def gen_company() -> str:
    return f"ООО Тест {secrets.token_hex(3)}"


def gen_key_name() -> str:
    return f"demo-{secrets.token_hex(3)}"


# ---------------------------------------------------------------- Chrome


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def launch_native_incognito() -> tuple[subprocess.Popen, Path, str]:
    chrome = find_chrome()
    port = _free_port()
    profile = Path(tempfile.mkdtemp(prefix="2gis-chrome-incognito-"))
    proc = subprocess.Popen(
        [
            str(chrome),
            *chrome_extra_args(),
            "--incognito",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=880,720",
            "--window-position=60,60",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp = f"http://127.0.0.1:{port}"
    deadline = time.time() + 25
    last = "нет ответа"
    while time.time() < deadline:
        if proc.poll() is not None:
            shutil.rmtree(profile, ignore_errors=True)
            raise SystemExit("Chrome инкогнито сразу закрылся")
        try:
            urllib.request.urlopen(f"{cdp}/json/version", timeout=1).read()
            return proc, profile, cdp
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
            time.sleep(0.2)
    proc.terminate()
    shutil.rmtree(profile, ignore_errors=True)
    raise SystemExit(f"не дождался Chrome CDP на {cdp}: {last}")


def stop_chrome(proc: subprocess.Popen | None, profile: Path | None, browser=None) -> None:
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if profile is not None:
        shutil.rmtree(profile, ignore_errors=True)


def pick_context(browser):
    for ctx in browser.contexts:
        if ctx.pages:
            return ctx, ctx.pages[0]
    ctx = browser.contexts[0]
    return ctx, ctx.new_page()


def try_click_turnstile(page) -> str:
    iframe = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]')
    try:
        if iframe.count():
            box = iframe.first.bounding_box()
            if box:
                page.mouse.click(box["x"] + 26, box["y"] + box["height"] / 2)
                return "iframe-checkbox"
            iframe.first.click(timeout=2000)
            return "iframe"
    except Exception:
        pass
    try:
        frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]').first
        frame.locator("body").click(timeout=2000)
        return "frame-body"
    except Exception:
        pass
    try:
        widget = page.locator(".cf-turnstile, [id^='cf-chl']").first
        if widget.count():
            widget.click(timeout=2000)
            return "widget"
    except Exception:
        pass
    return ""


def shot(page, out: Path, name: str) -> None:
    url = ""
    try:
        url = page.url
    except Exception:
        return
    if not url or url == "about:blank":
        return
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log(f"скрин: {path}")
    except Exception as exc:  # noqa: BLE001
        log(f"скрин не записался: {exc}")


def reset_session(context, page) -> None:
    try:
        context.clear_cookies()
    except Exception:
        pass
    try:
        page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
    except Exception:
        pass
    goto_retry(page, PLATFORM, tries=2, timeout=25_000, log=log)


# ---------------------------------------------------------------- почта / код


def pick_code(blob: str) -> str | None:
    if not blob:
        return None
    near = re.findall(
        r"(?:код|code|подтвержд|verification)[^\d]{0,80}(\d{6})",
        blob,
        re.I,
    )
    for code in near:
        if not _skip_code(code):
            return code
    for code in CODE_RE.findall(blob):
        if not _skip_code(code):
            return code
    return None


def _skip_code(code: str) -> bool:
    if code in {"000000", "123456", "111111"}:
        return True
    if code.startswith("202") and code[:4].isdigit() and 2020 <= int(code[:4]) <= 2035:
        return True
    return False


def wait_email_code(
    box: sm.Box,
    book: sm.Book | None,
    seen_ids: set[str],
    *,
    timeout: int,
    interval: float = 6,
) -> tuple[str, str | None]:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            messages = sm.fetch_inbox(box)
            if book:
                book.upsert(box)
        except sm.InboxError as exc:
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
            mid = str(msg.get("mid") or msg.get("id") or "")
            blob = sm.message_blob(msg)
            code = pick_code(blob)
            if not code and mid:
                try:
                    full = sm.fetch_message(box, mid)
                    blob = sm.message_blob(full)
                    code = pick_code(blob)
                except sm.InboxError as exc:
                    log(f"smailpro message {mid}: {exc}")
            if not code:
                continue
            blob_l = blob.lower()
            if any(w in blob_l for w in ("2gis", "2гис", "платформ", "platform", "код", "code")):
                return code, mid or None
            return code, mid or None
        left = int(deadline - time.time())
        log(f"письма с кодом нет (ящик {len(messages)}, новых {len(fresh)}), ещё {left}с…")
        time.sleep(interval)
    extra = f" последняя ошибка: {last_err}" if last_err else ""
    raise NeedUser(f"код из письма не пришёл за {timeout}с.{extra}", kind="no_code", email=box.address)


def take_mailbox(page, book: sm.Book, known: set[str], skip_extra: set[str]) -> sm.Box:
    for box in book.unused():
        addr = box.address.lower()
        if addr in known or addr in skip_extra:
            continue
        log(f"почта из книги: {box.address}")
        return box
    log("свободных ящиков в книге 2ГИС нет — создаю через SmailPro")
    try:
        box = sm.create_or_browser(
            page,
            log=log,
            notify=desktop_notify,
            click_turnstile=lambda: try_click_turnstile(page),
        )
    except sm.InboxError as exc:
        kind = "smailpro_captcha" if sm.looks_like_captcha(exc) else "no_mailbox"
        raise NeedUser(f"не смог взять почту: {exc}", kind=kind) from exc
    book.upsert(box)
    if box.address.lower() in known:
        raise NeedUser(f"{box.address} уже есть в ключах", kind="dup", email=box.address)
    return box


# ---------------------------------------------------------------- страница


def dismiss_banners(page) -> None:
    for name in (
        re.compile(r"принят|согласен|ok|хорошо|закрыть|accept|got it", re.I),
        re.compile(r"cookie", re.I),
    ):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            pass


def click_first(page, locators, *, timeout: int = 8_000) -> bool:
    for loc in locators:
        try:
            target = loc if hasattr(loc, "click") else page.locator(str(loc))
            if target.count() and target.first.is_visible():
                target.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def fill_input(page, name: str, value: str) -> bool:
    loc = page.locator(f'input[name="{name}"], textarea[name="{name}"]')
    try:
        if not loc.count():
            return False
        loc.first.click(timeout=4_000)
        loc.first.fill("")
        loc.first.fill(value)
        return True
    except Exception:
        return False


def fill_labeled(page, labels: tuple[str, ...], value: str) -> bool:
    for label in labels:
        try:
            loc = page.get_by_label(re.compile(label, re.I))
            if loc.count():
                loc.first.click(timeout=3_000)
                loc.first.fill(value)
                return True
        except Exception:
            continue
        try:
            loc = page.get_by_placeholder(re.compile(label, re.I))
            if loc.count():
                loc.first.fill(value)
                return True
        except Exception:
            continue
    return False


def page_says(page, pattern: str) -> bool:
    try:
        return bool(re.search(pattern, visible_text(page), re.I))
    except Exception:
        return False


def open_register(page) -> None:
    goto_retry(page, PLATFORM, log=log)
    page.wait_for_timeout(800)
    dismiss_banners(page)
    clicked = click_first(
        page,
        [
            page.get_by_test_id("landing-page-info-login-btn"),
            page.get_by_test_id("profile-button-unauthorized"),
            page.get_by_role("button", name=re.compile(r"Войти или зарегистрироваться", re.I)),
            page.get_by_role("link", name=re.compile(r"Войти или зарегистрироваться", re.I)),
        ],
    )
    if not clicked:
        log("кнопку входа не нашёл — открою OIDC напрямую через демо-ключ")
        goto_retry(page, DEMO_CREATE, log=log)
    try:
        page.wait_for_url(re.compile(re.escape(ID_HOST)), timeout=25_000)
    except PlaywrightTimeout:
        if ID_HOST not in page.url:
            shot(page, Path("screenshots"), "no-keycloak")
            raise NeedUser(f"не попал на Keycloak, url={page.url}", kind="no_form")
    page.wait_for_timeout(700)
    if page.locator('input[name="fullName"]').count():
        return
    click_first(
        page,
        [
            page.get_by_role("link", name=re.compile(r"Зарегистрироваться", re.I)),
            page.get_by_role("button", name=re.compile(r"Зарегистрироваться", re.I)),
            page.locator('a[href*="login-actions/registration"]'),
        ],
    )
    try:
        page.wait_for_selector('input[name="fullName"]', timeout=15_000)
    except PlaywrightTimeout:
        shot(page, Path("screenshots"), "no-register-form")
        raise NeedUser(f"нет формы регистрации, url={page.url}", kind="no_form")


def submit_register(page, *, full_name: str, phone: str, email: str, password: str) -> None:
    if not fill_input(page, "fullName", full_name) and not fill_labeled(page, ("ФИО", "full name"), full_name):
        raise NeedUser("нет поля ФИО", kind="no_form", email=email)
    if not fill_input(page, "phone", phone) and not fill_labeled(
        page, ("Номер телефона", "телефон", r"\+7"), phone
    ):
        raise NeedUser("нет поля телефона", kind="no_form", email=email)
    if not fill_input(page, "email", email) and not fill_labeled(
        page, ("Электронная почта", "email", "почта"), email
    ):
        raise NeedUser("нет поля почты", kind="no_form", email=email)
    if not fill_input(page, "password", password) and not fill_labeled(page, ("Пароль", "password"), password):
        raise NeedUser("нет поля пароля", kind="no_form", email=email)
    page.wait_for_timeout(400)
    if not click_first(
        page,
        [page.get_by_role("button", name=re.compile(r"^Зарегистрироваться$", re.I))],
    ):
        page.locator('button[type="submit"]').first.click(timeout=8_000)
    page.wait_for_timeout(1200)
    text = visible_text(page)
    if re.search(r"уже зарегистрирован|already registered|такой почтой", text, re.I):
        raise NeedUser(f"{email} уже занята на 2ГИС", kind="already_exists", email=email)
    if re.search(r"неверный формат|обязательное поле|небезопасный пароль", text, re.I):
        shot(page, Path("screenshots"), "register-validate")
        raise NeedUser(f"форма регистрации не приняла данные: {text[:180]}", kind="no_form", email=email)


def wait_code_form(page, email: str) -> None:
    deadline = time.time() + 25
    while time.time() < deadline:
        if page.locator('input[name="email_code"]').count():
            return
        if page_says(page, r"код из письма|подтверждение почты"):
            loc = page.get_by_label(re.compile(r"Код из письма", re.I))
            if loc.count():
                return
        page.wait_for_timeout(400)
    shot(page, Path("screenshots"), "no-email-code")
    raise NeedUser(f"нет формы кода, url={page.url}", kind="no_form", email=email)


def submit_email_code(page, code: str, email: str) -> None:
    filled = fill_input(page, "email_code", code)
    if not filled:
        filled = fill_labeled(page, ("Код из письма", "код"), code)
    if not filled:
        loc = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"]')
        if loc.count():
            loc.first.fill(code)
            filled = True
    if not filled:
        raise NeedUser("не нашёл поле кода", kind="no_form", email=email)
    page.wait_for_timeout(900)
    if page.locator('input[name="email_code"]').count() and ID_HOST in page.url:
        try:
            page.locator('input[name="email_code"]').press("Enter")
        except Exception:
            pass
        page.wait_for_timeout(800)


def _click_industry_option(page) -> bool:
    for name in ("IT", "ИТ", "Information Technology", "Информационные технологии"):
        try:
            opt = page.get_by_role("option", name=re.compile(rf"^{re.escape(name)}$", re.I))
            if opt.count() and opt.first.is_visible():
                opt.first.click(timeout=2_000)
                log(f"отрасль: {name}")
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(name, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2_000)
                log(f"кликнул «{name}»")
                return True
        except Exception:
            continue
    return False


def maybe_pick_industry(page) -> None:
    if _click_industry_option(page):
        return
    if not page_says(page, r"отрасл|сфер|деятельн|industry|категор"):
        return
    for trigger in (
        page.get_by_label(re.compile(r"отрасл|сфер|деятельн|industry|категор", re.I)),
        page.get_by_role("combobox"),
    ):
        try:
            if trigger.count() and trigger.first.is_visible():
                trigger.first.click(timeout=2_000)
                page.wait_for_timeout(400)
                _click_industry_option(page)
                return
        except Exception:
            continue


def accept_agreements(page) -> None:
    try:
        boxes = page.get_by_role("checkbox")
        for i in range(min(boxes.count(), 4)):
            box = boxes.nth(i)
            if box.is_visible() and not box.is_checked():
                box.check(timeout=2_000)
    except Exception:
        pass
    click_first(
        page,
        [
            page.get_by_text(re.compile(r"оферт|персональн|политик конфиден", re.I)),
        ],
        timeout=2_000,
    )


def fill_company(page, company: str, email: str) -> None:
    deadline = time.time() + 45
    saw_company = False
    while time.time() < deadline:
        url = page.url
        if "platform.2gis.ru" in url and any(p in url for p in ("/dashboard", "/keys", "/companies")):
            if page.get_by_text(re.compile(r"Название компании", re.I)).count():
                saw_company = True
                break
            if "/dashboard" in url or "/keys" in url:
                log("компания уже есть — кабинет открыт")
                return
        if page.get_by_text(re.compile(r"Название компании|Добавление компании|Добавьте компанию", re.I)).count():
            saw_company = True
            break
        if ID_HOST in url:
            page.wait_for_timeout(500)
            continue
        page.wait_for_timeout(400)
    if not saw_company:
        if "platform.2gis.ru" in page.url:
            log(f"формы компании нет, url={page.url} — пробую дальше")
            return
        shot(page, Path("screenshots"), "no-company")
        raise NeedUser(f"не дошёл до компании, url={page.url}", kind="no_company", email=email)

    filled = fill_labeled(page, ("Название компании", "компании"), company)
    if not filled:
        filled = fill_input(page, "name", company) or fill_input(page, "companyName", company)
    if not filled:
        loc = page.locator('input[type="text"]:visible')
        if loc.count():
            loc.first.fill(company)
            filled = True
    if not filled:
        shot(page, Path("screenshots"), "company-no-input")
        raise NeedUser("нет поля названия компании", kind="no_company", email=email)

    maybe_pick_industry(page)
    accept_agreements(page)
    page.wait_for_timeout(300)
    if not click_first(
        page,
        [
            page.get_by_role("button", name=re.compile(r"Добавить компанию", re.I)),
            page.get_by_role("button", name=re.compile(r"Добавить компанию.*войти", re.I)),
            page.get_by_role("button", name=re.compile(r"^Добавить$", re.I)),
        ],
    ):
        shot(page, Path("screenshots"), "company-no-submit")
        raise NeedUser("нет кнопки «Добавить компанию»", kind="no_company", email=email)
    try:
        page.wait_for_url(re.compile(r"platform\.2gis\.ru/ru/(dashboard|keys)"), timeout=30_000)
    except PlaywrightTimeout:
        if "platform.2gis.ru" not in page.url:
            shot(page, Path("screenshots"), "after-company")
            raise NeedUser(f"после компании не кабинет, url={page.url}", kind="no_cabinet", email=email)
    log(f"компания «{company}», url={page.url}")


class KeySniffer:
    def __init__(self) -> None:
        self.candidates: list[str] = []
        self._page = None

    def attach(self, page) -> None:
        self._page = page
        page.on("response", self._on_response)

    def detach(self) -> None:
        page = self._page
        self._page = None
        if page is None:
            return
        try:
            page.remove_listener("response", self._on_response)
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        if self._page is None:
            return
        try:
            url = resp.url
            if "keys.api.2gis.com" not in url:
                return
            if resp.request.method not in ("GET", "POST", "PUT", "PATCH"):
                return
            if resp.status not in (200, 201):
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            text = resp.text()
        except Exception:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            for hit in UUID_RE.findall(text):
                self.candidates.append(hit)
            return
        self._walk(data)

    def _walk(self, node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                self._walk(v, str(k))
        elif isinstance(node, list):
            for item in node:
                self._walk(item, key)
        elif isinstance(node, str) and UUID_RE.fullmatch(node):
            if key.lower() in {"key", "apikey", "api_key", "token", "value", "uuid", "id", "demoKey"}:
                self.candidates.append(node)
            elif key.lower() in {"keyid", "user_id", "userid", "sessionid"}:
                return
            else:
                self.candidates.append(node)


def extract_key_from_page(page) -> str | None:
    try:
        text = visible_text(page)
    except Exception:
        text = ""
    hits = UUID_RE.findall(text)
    if hits:
        # ключ на странице ключа обычно не первый UUID в куках, а видимый блок
        return hits[-1] if len(hits) > 1 else hits[0]
    try:
        values = page.evaluate(
            """() => [...document.querySelectorAll('input, code, pre, textarea')]
                .map((el) => (el.value || el.textContent || '').trim())
                .filter(Boolean)"""
        )
    except Exception:
        values = []
    for val in values or []:
        m = UUID_RE.search(str(val))
        if m:
            return m.group(0)
    return None


def create_demo_key(page, key_name: str, email: str, sniffer: KeySniffer) -> tuple[str, str]:
    dismiss_banners(page)
    if "keys/create/demo" not in page.url:
        clicked = click_first(
            page,
            [
                page.get_by_test_id("landing-page-create-key-button"),
                page.get_by_role("button", name=re.compile(r"Создать демо-ключ", re.I)),
                page.get_by_role("link", name=re.compile(r"Создать демо-ключ", re.I)),
            ],
        )
        if not clicked:
            goto_retry(page, DEMO_CREATE, log=log)
        page.wait_for_timeout(800)
    if ID_HOST in page.url:
        raise NeedUser("создание ключа ушло на логин — сессии нет", kind="no_cabinet", email=email)
    try:
        page.wait_for_url(re.compile(r"keys/create/demo"), timeout=20_000)
    except PlaywrightTimeout:
        if "keys/create" not in page.url:
            log(f"не /keys/create/demo, url={page.url} — всё равно заполняю имя")

    filled = fill_labeled(page, ("Название ключа", "название"), key_name)
    if not filled:
        filled = fill_input(page, "name", key_name) or fill_input(page, "title", key_name)
    if not filled:
        loc = page.locator('input[type="text"]:visible')
        if loc.count():
            loc.first.fill(key_name)
            filled = True
    if not filled:
        shot(page, Path("screenshots"), "demo-no-name")
        raise NeedUser("нет поля названия ключа", kind="no_key", email=email)

    before = list(sniffer.candidates)
    if not click_first(
        page,
        [
            page.get_by_role("button", name=re.compile(r"^Создать$", re.I)),
            page.get_by_role("button", name=re.compile(r"Создать демо", re.I)),
        ],
    ):
        shot(page, Path("screenshots"), "demo-no-submit")
        raise NeedUser("нет кнопки «Создать»", kind="no_key", email=email)

    try:
        page.wait_for_url(re.compile(r"/keys/\d+"), timeout=30_000)
    except PlaywrightTimeout:
        page.wait_for_timeout(1500)

    key = None
    fresh = [k for k in sniffer.candidates if k not in before]
    if fresh:
        key = fresh[-1]
    if not key:
        key = extract_key_from_page(page)
    if not key:
        shot(page, Path("screenshots"), "demo-no-uuid")
        raise NeedUser(f"не вижу UUID ключа, url={page.url}", kind="no_key", email=email)
    key_url = page.url
    log(f"демо-ключ {key[:8]}… url={key_url}")
    return key, key_url


def register_one(
    page,
    book: sm.Book,
    *,
    index: int,
    total: int,
    known: set[str],
    accounts: Path,
    keys_json: Path,
    shots: Path,
    mail_timeout: int,
    skip_extra: set[str],
    dry_run: bool,
) -> Outcome:
    started = time.time()
    log(f"— {index}/{total} беру почту")
    box = take_mailbox(page, book, known, skip_extra)
    email = box.address
    password = gen_password()
    full_name = gen_full_name()
    phone = gen_phone()
    company = gen_company()
    key_name = gen_key_name()
    log(f"{email}: {full_name}, {phone}, компания «{company}»")

    open_register(page)
    submit_register(page, full_name=full_name, phone=phone, email=email, password=password)
    if dry_run:
        log("dry-run: форму отправил бы, дальше стоп")
        return Outcome(index, email, "dry_run", time.time() - started)

    wait_code_form(page, email)
    log(f"{email}: жду код из письма до {mail_timeout}с")
    seen: set[str] = set()
    code, mid = wait_email_code(box, book, seen, timeout=mail_timeout)
    if mid:
        seen.add(mid)
    log(f"{email}: код {code}")
    submit_email_code(page, code, email)

    fill_company(page, company, email)
    sniffer = KeySniffer()
    sniffer.attach(page)
    try:
        key, key_url = create_demo_key(page, key_name, email, sniffer)
    finally:
        sniffer.detach()

    rec = {
        "email": email,
        "password": password,
        "full_name": full_name,
        "phone": phone,
        "company": company,
        "key_name": key_name,
        "key": key,
        "key_url": key_url,
        "created_at": now_iso(),
    }
    append_jsonl(accounts, rec)
    rewrite_keys_json(keys_json, accounts)
    known.add(email.lower())
    book.mark(email, "2gis", has_account=True)
    return Outcome(index, email, "ok", time.time() - started, key_url, key)


def print_summary(outcomes: list[Outcome], accounts: Path, keys_json: Path) -> None:
    print()
    print(f"{'#':>3}  {'статус':<16} {'сек':>5}  {'почта':<36} ключ")
    print("-" * 100)
    ok = 0
    for o in outcomes:
        shown = f"{o.key[:8]}…" if o.key else o.note[:40]
        print(f"{o.index:3d}  {o.status:<16} {o.seconds:5.0f}  {o.email:<36} {shown}")
        if o.status == "ok":
            ok += 1
    print("-" * 100)
    print(f"успешно {ok}/{len(outcomes)}  jsonl={accounts}  json={keys_json}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Регистрация 2ГИС Platform и демо-ключи")
    p.add_argument("--count", type=int, help="сколько ключей; с флагом не спрашивает")
    p.add_argument("--accounts", type=Path, default=Path("2gis-keys.jsonl"), help="полные записи, JSONL")
    p.add_argument(
        "--keys",
        type=Path,
        default=Path("2gis-demo-keys.json"),
        help="JSON: массив UUID и аккаунты",
    )
    p.add_argument("--smail-book", type=Path, default=Path("2gis_book.json"))
    p.add_argument("--shots", type=Path, default=Path("screenshots"))
    p.add_argument("--log-dir", type=Path, default=Path("runs"))
    p.add_argument("--mail-timeout", type=int, default=180)
    p.add_argument("--dry-run", action="store_true", help="дойти до формы, не ждать письмо")
    p.add_argument("-y", "--yes", action="store_true", help="без вопросов")
    return p.parse_args()


def main() -> int:
    global AUTO_YES
    args = parse_args()
    AUTO_YES = bool(args.yes or args.count is not None)
    log_path = setup_log_file(args.log_dir if str(args.log_dir) else None)
    if log_path:
        log(f"лог пишу в {log_path}")

    count = ask_count(args.count)
    book = sm.Book(args.smail_book)
    known = known_emails(args.accounts)
    log(
        f"план: {count} ключей → {args.accounts} + {args.keys}; "
        f"книга {args.smail_book} свободных {len(book.unused())}; уже записано {len(known)}"
        f"{' [DRY-RUN]' if args.dry_run else ''}"
    )
    if sm.touch_session():
        log("SmailPro: сессию пнул")
    elif sm.load_cookies():
        log("SmailPro: куки есть, сайт сессию не продлил — при create открою Chrome")
    else:
        log("SmailPro: кук нет — при create открою Chrome")

    outcomes: list[Outcome] = []
    chrome_proc, profile, cdp = launch_native_incognito()
    browser = None
    log(f"инкогнито Chrome слушает {cdp}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp)
            context, page = pick_context(browser)

            def restart_chrome(reason: str) -> None:
                nonlocal chrome_proc, profile, browser, context, page
                log(f"перезапускаю Chrome: {reason}")
                stop_chrome(chrome_proc, profile, browser)
                chrome_proc, profile, cdp_now = launch_native_incognito()
                browser = p.chromium.connect_over_cdp(cdp_now)
                context, page = pick_context(browser)
                log(f"новый инкогнито слушает {cdp_now}")

            i = 1
            auto_retries = 0
            recent_skip: set[str] = set()
            while i <= count:
                try:
                    reset_session(context, page)
                except Exception as exc:  # noqa: BLE001
                    log(f"сброс сессии не вышел ({exc}) — новый Chrome")
                    restart_chrome("сброс сессии сломался")
                started = time.time()
                try:
                    outcome = register_one(
                        page,
                        book,
                        index=i,
                        total=count,
                        known=known,
                        accounts=args.accounts,
                        keys_json=args.keys,
                        shots=args.shots,
                        mail_timeout=args.mail_timeout,
                        skip_extra=recent_skip,
                        dry_run=args.dry_run,
                    )
                    outcomes.append(outcome)
                    auto_retries = 0
                    log(f"готово {sum(1 for o in outcomes if o.status == 'ok')}/{count}")
                    i += 1
                except NeedUser as exc:
                    email = exc.email or ""
                    if email:
                        recent_skip.add(email.lower())
                    can_auto = exc.kind in AUTO_RETRY_KINDS and (AUTO_YES or auto_retries < MAX_AUTO_RETRIES)
                    log(f"{email or '—'}: {exc}")
                    if can_auto:
                        auto_retries += 1
                        log(f"ещё раз ту же попытку ({auto_retries}), kind={exc.kind}")
                        if exc.kind in {"captcha", "smailpro_captcha", "no_cabinet", "no_form"}:
                            restart_chrome(exc.kind)
                        continue
                    outcomes.append(Outcome(i, email, exc.kind, time.time() - started, str(exc)))
                    i += 1
                except (PlaywrightTimeout, PlaywrightError) as exc:
                    log(f"браузер: {exc}")
                    shot(page, args.shots, "playwright")
                    outcomes.append(Outcome(i, "", "error", time.time() - started, repr(exc)[:120]))
                    restart_chrome("ошибка playwright")
                    i += 1
                except Exception as exc:  # noqa: BLE001
                    log(f"ошибка: {exc!r}")
                    shot(page, args.shots, "error")
                    outcomes.append(Outcome(i, "", "error", time.time() - started, repr(exc)[:120]))
                    i += 1
    finally:
        stop_chrome(chrome_proc, profile, browser)

    print_summary(outcomes, args.accounts, args.keys)
    ok = sum(1 for o in outcomes if o.status == "ok")
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nстоп.", file=sys.stderr)
        raise SystemExit(130)

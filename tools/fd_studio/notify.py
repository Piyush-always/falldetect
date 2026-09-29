"""
Phone alerts: a Telegram message, and a Telegram voice call via CallMeBot.

WHY THESE TWO
-------------
Free, instant, and no business verification. The Telegram Bot API is official
and reliable, but a message is only a notification and can be muted. The
CallMeBot call actually RINGS the phone and reads the alert aloud - that is
the part that wakes someone - but it is a free third-party service for
personal use with no guarantee behind it. So the message is the channel of
record and the call is the loud one; neither is trusted alone.

WHAT THIS IS NOT
----------------
Sent from the laptop, over the laptop's internet. Laptop asleep, offline, or
FD Studio closed means nothing is sent - which is why every send reports
success or failure back to the screen, and a failure is shown as "NOT sent",
never swallowed.

Deliberately no Qt import (same rule as engine.py): sends run on a plain
thread and report through a queue the GUI drains on its timer, so a slow
network can never freeze the window mid-alarm. Standard library only, so the
packaged exe gains no dependency.

SECRETS
-------
The bot token and contacts live in %APPDATA%\\FD Studio\\alerts.json - outside
the repo (cannot be committed) and outside the exe (cannot be shared with it).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = (Path(os.environ.get("APPDATA") or Path.home())
               / "FD Studio" / "alerts.json")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CALLMEBOT_CALL = "https://api.callmebot.com/start.php"

#: CallMeBot reads at most 256 characters.
CALL_TEXT_MAX = 256

#: Waits between attempts. A fall alert is worth retrying through a brief
#: Wi-Fi drop; three tries over ~7 s, then report the failure loudly.
RETRY_DELAYS_S = (2.0, 5.0)
HTTP_TIMEOUT_S = 10.0

_TEMPLATE = {
    "_help": [
        "FD Studio phone alerts. Fill this in, save, then press "
        "'Send test to phones' on the User tab.",
        "wearer_name: how the alerts refer to the wearer, e.g. 'Grandma'.",
        "telegram_bot_token: in Telegram, message @BotFather, send /newbot, "
        "and paste the token it gives you.",
        "telegram_chat_ids: leave empty. Each person (or a family group with "
        "the bot added) sends any message to your bot first; 'Send test to "
        "phones' then finds and fills these in.",
        "callmebot_users: Telegram usernames to CALL, e.g. '@alice'. Each "
        "person must first send /start to @CallMeBot_txtbot in Telegram.",
        "Free services, no guarantee. Alerts go out only while this laptop "
        "is on, online, and FD Studio is open.",
    ],
    "wearer_name": "",
    "telegram_bot_token": "",
    "telegram_chat_ids": [],
    "callmebot_users": [],
}


@dataclass
class AlertConfig:
    wearer_name: str = ""
    telegram_bot_token: str = ""
    telegram_chat_ids: list[str] = field(default_factory=list)
    callmebot_users: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.wearer_name.strip() or "the wearer"

    @property
    def configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_ids) \
            or bool(self.callmebot_users)


def load_config(path: Path = CONFIG_PATH) -> tuple[AlertConfig, str]:
    """(config, problem). A broken file is reported, never guessed at."""
    if not path.exists():
        return AlertConfig(), ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return AlertConfig(), f"cannot read {path.name}: {exc}"
    if not isinstance(raw, dict):
        return AlertConfig(), f"{path.name} is not a JSON object"
    cfg = AlertConfig(
        wearer_name=str(raw.get("wearer_name", "")),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")).strip(),
        telegram_chat_ids=[str(c).strip() for c in
                           raw.get("telegram_chat_ids", []) if str(c).strip()],
        callmebot_users=[str(u).strip() for u in
                         raw.get("callmebot_users", []) if str(u).strip()],
    )
    return cfg, ""


def ensure_config_file(path: Path = CONFIG_PATH) -> Path:
    """Create the template if missing, so 'open settings' has something
    to open. Never overwrites an existing file."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_TEMPLATE, indent=2), encoding="utf-8")
    return path


def _save_chat_ids(path: Path, chat_ids: list[str]) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["telegram_chat_ids"] = chat_ids
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _visible_text(html: bytes) -> str:
    """The words on an HTML page, without tags or scripts."""
    s = html.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return " ".join(s.split())


def _http(url: str, data: dict | None = None) -> bytes:
    """GET (data None) or form POST. Raises RuntimeError with the service's
    own error text, because 'HTTP 400' alone does not say what to fix."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body,
                                 headers={"User-Agent": "FD-Studio"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("description", detail)
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"no connection ({reason})") from None


class Notifier:
    """Fire-and-report phone alerts. Safe to call from the GUI thread.

    results carries ("log", text) and ("done", tag, ok, failures) tuples:
    ok is the number of deliveries the services accepted, failures a list of
    "who: why" strings. "Accepted" is all that can be known - a call can
    ring unanswered, a message can sit unread.
    """

    def __init__(self, config_path: Path = CONFIG_PATH, http=_http) -> None:
        self.config_path = config_path
        self.results: queue.Queue = queue.Queue()
        self._http = http
        # ONE worker, in order. With a thread per send, "alert dismissed"
        # could reach a phone before the alert it refers to.
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True,
                         name="notify").start()

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception as exc:  # noqa: BLE001 - must never kill the worker
                self.results.put(("log", f"phone alert: internal error {exc!r}"))

    def config(self) -> tuple[AlertConfig, str]:
        return load_config(self.config_path)

    def describe(self) -> str:
        """One line for the screen: what will happen on an alert."""
        cfg, problem = self.config()
        if problem:
            return f"Phone alerts: settings file broken — {problem}"
        if not cfg.configured:
            return "Phone alerts: not set up — nobody is contacted"
        parts = []
        if cfg.telegram_bot_token and cfg.telegram_chat_ids:
            parts.append(f"Telegram message to {len(cfg.telegram_chat_ids)} "
                         f"chat{'s' if len(cfg.telegram_chat_ids) != 1 else ''}")
        if cfg.callmebot_users:
            parts.append("call " + ", ".join(cfg.callmebot_users))
        return "Phone alerts: " + " + ".join(parts)

    # ── sending ──────────────────────────────────────────────────────────────
    def send(self, tag: str, text: str, call_text: str | None = None,
             silent: bool = False) -> bool:
        """Start sending. False (and nothing sent) if not set up.

        call_text None means message only - follow-ups and 'all OK' notices
        must never ring someone's phone at 3am.
        """
        cfg, problem = self.config()
        if problem or not cfg.configured:
            return False
        self._jobs.put(lambda: self._send_all(cfg, tag, text, call_text, silent))
        return True

    def send_test(self) -> None:
        """Queue a test message and call. Finds chat ids first if the file
        has a bot token but none yet. Reports as tag "test"."""
        self._jobs.put(self._test_job)

    def _test_job(self) -> None:
        cfg, problem = self.config()
        if problem:
            self.results.put(("done", "test", 0, [problem]))
            return
        if cfg.telegram_bot_token and not cfg.telegram_chat_ids:
            try:
                found = self.discover_chats()
            except RuntimeError as exc:
                self.results.put(("done", "test", 0, [f"Telegram: {exc}"]))
                return
            if not found:
                self.results.put(("done", "test", 0, [
                    "Telegram: nobody has messaged the bot yet — send it any "
                    "message from each phone (or add it to the family group), "
                    "then press Send test again"]))
                return
            for chat_id, title in found:
                self.results.put(("log", f"phone alerts: added Telegram chat "
                                         f"'{title}' ({chat_id})"))
            cfg, _ = self.config()
        if not cfg.configured:
            self.results.put(("done", "test", 0, [
                "not set up — press 'Phone alert settings' and fill in the file"]))
            return
        self._send_all(cfg, "test",
                       f"🧪 Test from FD Studio: phone alerts for {cfg.name} "
                       f"are working.",
                       "This is a test from F D Studio. Phone alerts are working.",
                       silent=False)

    def _send_all(self, cfg: AlertConfig, tag: str, text: str,
                  call_text: str | None, silent: bool) -> None:
        jobs = []
        if cfg.telegram_bot_token:
            for chat in cfg.telegram_chat_ids:
                jobs.append((f"Telegram {chat}",
                             lambda c=chat: self._telegram(cfg, c, text, silent)))
        if call_text:
            for user in cfg.callmebot_users:
                jobs.append((f"call {user}",
                             lambda u=user: self._call(u, call_text)))

        ok, failures = 0, []
        for who, job in jobs:
            err, detail = self._with_retries(job)
            if err is None:
                ok += 1
                self.results.put(("log", f"phone alert [{tag}]: {who} — accepted"
                                         + (f" ({detail})" if detail else "")))
            else:
                failures.append(f"{who}: {err}")
                self.results.put(("log", f"phone alert [{tag}]: {who} — FAILED: {err}"))
        self.results.put(("done", tag, ok, failures))

    def _with_retries(self, job) -> tuple[str | None, str]:
        """(error or None, detail from the service)."""
        last = ""
        for delay in (0.0,) + RETRY_DELAYS_S:
            if delay:
                time.sleep(delay)
            try:
                return None, job() or ""
            except RuntimeError as exc:
                last = str(exc)
                # A wrong token, unknown chat or unauthorised call will not
                # fix itself by retrying.
                if last.startswith(("HTTP 400", "HTTP 401", "HTTP 403",
                                    "HTTP 404", "CallMeBot refused")):
                    break
        return last, ""

    def _telegram(self, cfg: AlertConfig, chat: str, text: str,
                  silent: bool) -> None:
        raw = self._http(
            TELEGRAM_API.format(token=cfg.telegram_bot_token, method="sendMessage"),
            {"chat_id": chat, "text": text,
             "disable_notification": "true" if silent else "false"})
        if not json.loads(raw or b"{}").get("ok"):
            raise RuntimeError(f"Telegram refused: {raw[:200]!r}")

    def _call(self, user: str, call_text: str) -> str:
        # cc=missed: a text copy only if the call is not picked up - the
        # Telegram message already carries the details.
        query = urllib.parse.urlencode({
            "user": user, "text": call_text[:CALL_TEXT_MAX],
            "rpt": "2", "cc": "missed",
        })
        raw = self._http(f"{CALLMEBOT_CALL}?{query}")
        text = _visible_text(raw)
        # CallMeBot answers HTTP 200 whether or not it can call, and says
        # why in the page. Verified 2026-09-29 for an unauthorised user:
        # "Authorization for user @x is not received. Warning! User not
        # authorized." Other failure wordings are not known, so the page
        # text goes into the log for a human to read.
        if "not authorized" in text.lower() or "is not received" in text.lower():
            raise RuntimeError("CallMeBot refused — this person must first "
                               "send /start to @CallMeBot_txtbot in Telegram")
        return f"CallMeBot: {text[-160:]}" if text else ""

    # ── setup helper ─────────────────────────────────────────────────────────
    def discover_chats(self) -> list[tuple[str, str]]:
        """Chats that have messaged the bot, as (chat_id, title).

        Fills telegram_chat_ids when it is empty, so nobody has to find a
        numeric id by hand. Raises RuntimeError with the reason on failure.
        """
        cfg, problem = self.config()
        if problem:
            raise RuntimeError(problem)
        if not cfg.telegram_bot_token:
            raise RuntimeError("no telegram_bot_token in the settings file")
        raw = self._http(TELEGRAM_API.format(token=cfg.telegram_bot_token,
                                             method="getUpdates"))
        found: dict[str, str] = {}
        for upd in json.loads(raw).get("result", []):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                title = (chat.get("title") or " ".join(
                    filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    or chat.get("username") or "?")
                found[str(chat["id"])] = title
        if found and not cfg.telegram_chat_ids:
            _save_chat_ids(self.config_path, list(found))
        return list(found.items())

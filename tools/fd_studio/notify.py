"""
Phone alerts: a Telegram message, and a Telegram voice call via CallMeBot.

WHO GETS THEM
-------------
Anyone who opens the bot in Telegram and presses Start. FD Studio listens to
the bot while it runs: /start (or any first message) adds that chat to the
alert list and replies with a welcome, /stop removes it. Adding the bot to a
family group subscribes the whole group. Nobody edits a file by hand.

Anyone who finds the bot can subscribe - bot names are searchable - so every
new subscriber is announced (silently) to everyone already on the list. A
stranger joining is then seen, not hidden.

WHY THESE TWO SERVICES
----------------------
Free, instant, and no business verification. The Telegram Bot API is official
and reliable, but a message is only a notification and can be muted. A bot
cannot place calls, so the CallMeBot call is what RINGS the phone and reads
the alert aloud - a free third-party service for personal use with no
guarantee. It needs each person to press Start on @CallMeBot_txtbot once and
to have a Telegram username; the welcome message says so, and a person whose
call is refused gets told again. The message is the channel of record; the
call is the loud one; neither is trusted alone.

WHAT THIS IS NOT
----------------
Sent from the laptop, over the laptop's internet. Laptop asleep, offline, or
FD Studio closed means nothing is sent - and new subscribers are not picked
up either. Every send reports success or failure back to the screen; a
failure is shown as "NOT sent", never swallowed. Only ONE running FD Studio
may use a bot: Telegram hands each update to one listener, so two would split
the subscribers between them.

Deliberately no Qt import (same rule as engine.py): sends and the listener
run on plain threads and report through a queue the GUI drains on its timer.
Standard library only, so the packaged exe gains no dependency.

SECRETS
-------
The bot token and the subscriber list live in %USERPROFILE%\\.fd_studio\\
alerts.json - outside the repo, so they cannot be committed (and not in
AppData: see CONFIG_PATH). A shipped exe can carry a
token (tools/build_exe.ps1 bundles alerts.bundle.json, which is git-ignored);
on first run it seeds the settings file from that. Such an exe contains the
token: share it privately, never commit or publish it.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: In the home folder, NOT %APPDATA%. The Microsoft Store Python (what
#: fd_studio.ps1 finds on this machine) is an MSIX-packaged app, and Windows
#: silently redirects its AppData writes to a private copy under
#: AppData\Local\Packages\...\LocalCache. The source-run tool and the exe then
#: read two different alerts.json files - measured 2026-09-30: the exe held
#: the token and a subscriber, the source run an empty template, so an SOS
#: reported "not set up". The home folder is not virtualised.
#: FD_STUDIO_ALERTS_FILE overrides the location - for tests, so they can
#: never touch (or poll with) a real bot.
CONFIG_PATH = Path(os.environ.get("FD_STUDIO_ALERTS_FILE") or
                   (Path.home() / ".fd_studio" / "alerts.json"))

#: Name of the token file build_exe.ps1 bundles into a shipped exe.
BUNDLE_NAME = "alerts.bundle.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CALLMEBOT_CALL = "https://api.callmebot.com/start.php"
CALLMEBOT_BOT = "@CallMeBot_txtbot"

#: CallMeBot reads at most 256 characters.
CALL_TEXT_MAX = 256

#: Waits between attempts. A fall alert is worth retrying through a brief
#: Wi-Fi drop; three tries over ~7 s, then report the failure loudly.
RETRY_DELAYS_S = (2.0, 5.0)
HTTP_TIMEOUT_S = 10.0
#: getUpdates long-poll. Below HTTP_TIMEOUT_S, so a quiet bot is not an error.
POLL_TIMEOUT_S = 8

_TEMPLATE = {
    "_help": [
        "FD Studio phone alerts.",
        "wearer_name: how the alerts refer to the wearer, e.g. 'Grandma'.",
        "telegram_bot_token: from @BotFather in Telegram (/newbot).",
        "subscribers: filled in automatically when someone presses Start on "
        "the bot, removed when they send /stop. No need to edit.",
        "telegram_chat_ids / callmebot_users: optional extras added by hand.",
        "Free services, no guarantee. Alerts go out only while this laptop "
        "is on, online, and FD Studio is open.",
    ],
    "wearer_name": "",
    "telegram_bot_token": "",
    "subscribers": [],
    "telegram_chat_ids": [],
    "callmebot_users": [],
}


def _template() -> dict:
    """A fresh copy. dict(_TEMPLATE) would share its lists, and the first
    subscriber appended would be added to the template itself."""
    return json.loads(json.dumps(_TEMPLATE))


@dataclass
class AlertConfig:
    wearer_name: str = ""
    telegram_bot_token: str = ""
    #: [{"chat_id", "name", "username" ("@x" or ""), "group": bool}]
    subscribers: list[dict] = field(default_factory=list)
    telegram_chat_ids: list[str] = field(default_factory=list)
    callmebot_users: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.wearer_name.strip() or "the wearer"

    @property
    def message_targets(self) -> list[str]:
        ids = [s["chat_id"] for s in self.subscribers] + self.telegram_chat_ids
        return list(dict.fromkeys(ids))

    @property
    def call_targets(self) -> list[str]:
        users = [s["username"] for s in self.subscribers
                 if s.get("username") and not s.get("group")]
        return list(dict.fromkeys(users + self.callmebot_users))

    @property
    def configured(self) -> bool:
        return bool(self.telegram_bot_token and self.message_targets) \
            or bool(self.call_targets)


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
    subs = [s for s in raw.get("subscribers", [])
            if isinstance(s, dict) and str(s.get("chat_id", "")).strip()]
    cfg = AlertConfig(
        wearer_name=str(raw.get("wearer_name", "")),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")).strip(),
        subscribers=[{"chat_id": str(s["chat_id"]).strip(),
                      "name": str(s.get("name", "")),
                      "username": str(s.get("username", "")),
                      "group": bool(s.get("group", False))} for s in subs],
        telegram_chat_ids=[str(c).strip() for c in
                           raw.get("telegram_chat_ids", []) if str(c).strip()],
        callmebot_users=[str(u).strip() for u in
                         raw.get("callmebot_users", []) if str(u).strip()],
    )
    return cfg, ""


def _write_json(path: Path, raw: dict) -> None:
    """Write via a temp file and rename: a crash mid-write must not leave a
    half-written settings file that loses every subscriber."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _bundled_defaults() -> dict:
    """Token and wearer name baked into a shipped exe, or {}."""
    base = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not base:
        return {}
    try:
        raw = json.loads((Path(base) / BUNDLE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def seed_from_bundle(path: Path = CONFIG_PATH) -> bool:
    """First run of a shipped exe: create the settings from the bundled
    token, so the person receiving it never opens a settings file. Only
    fills a missing token; never touches subscribers or an existing one."""
    bundle = _bundled_defaults()
    token = str(bundle.get("telegram_bot_token", "")).strip()
    if not token:
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() \
            else _template()
    except (OSError, ValueError):
        return False            # a broken file is reported by load_config
    if str(raw.get("telegram_bot_token", "")).strip():
        return False
    raw["telegram_bot_token"] = token
    if not str(raw.get("wearer_name", "")).strip():
        raw["wearer_name"] = str(bundle.get("wearer_name", ""))
    _write_json(path, raw)
    return True


def ensure_config_file(path: Path = CONFIG_PATH) -> Path:
    """Create the template if missing, so 'open settings' has something
    to open. Never overwrites an existing file."""
    if not path.exists():
        _write_json(path, _template())
    return path


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
    """Fire-and-report phone alerts, plus the bot's subscription inbox.
    Safe to call from the GUI thread.

    results carries ("log", text) and ("done", tag, ok, failures) tuples:
    ok is the number of deliveries the services accepted, failures a list of
    "who: why" strings. "Accepted" is all that can be known - a call can
    ring unanswered, a message can sit unread.
    """

    def __init__(self, config_path: Path = CONFIG_PATH, http=_http) -> None:
        self.config_path = config_path
        self.results: queue.Queue = queue.Queue()
        self._http = http
        #: "@name" of the bot, learnt by the inbox; for the share link.
        self.bot_username = ""
        # Serialises edits to the settings file (inbox vs. anything else).
        self._file_lock = threading.Lock()
        # ONE sender, in order. With a thread per send, "alert dismissed"
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

    @property
    def share_link(self) -> str:
        return f"t.me/{self.bot_username.lstrip('@')}" if self.bot_username else ""

    def describe(self) -> str:
        """For the screen: who gets alerted, and how to add people."""
        cfg, problem = self.config()
        if problem:
            return f"Phone alerts: settings file broken — {problem}"
        if not cfg.telegram_bot_token and not cfg.configured:
            return "Phone alerts: not set up — nobody is contacted"
        share = (f"\nTo subscribe, open {self.share_link} in Telegram and "
                 f"press Start." if self.share_link else "")
        if not cfg.configured:
            return "Phone alerts: nobody has subscribed yet." + share
        names = [s["name"] or s["username"] or s["chat_id"]
                 for s in cfg.subscribers]
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3}"
                                         if len(names) > 3 else "")
        n = len(cfg.message_targets)
        line = f"Phone alerts: {n} on Telegram"
        if shown:
            line += f" ({shown})"
        if cfg.call_targets:
            line += f" · calls to {len(cfg.call_targets)}"
        return line + share

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
        """Queue a test message and call to everyone. Reports as "test"."""
        self._jobs.put(self._test_job)

    def _test_job(self) -> None:
        cfg, problem = self.config()
        if problem:
            self.results.put(("done", "test", 0, [problem]))
            return
        if not cfg.configured:
            how = (f"send people the link {self.share_link} and ask them to "
                   f"press Start" if self.share_link else
                   "press 'Phone alert settings' and add the bot token")
            self.results.put(("done", "test", 0,
                              [f"nobody to alert yet — {how}"]))
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
            for chat in cfg.message_targets:
                jobs.append((f"Telegram {self._who(cfg, chat)}",
                             lambda c=chat: self._telegram(cfg, c, text, silent)))
        if call_text:
            for user in cfg.call_targets:
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
                if who.startswith("call ") and err.startswith("CallMeBot refused"):
                    self._tell_call_blocked(cfg, who[5:], err)
        self.results.put(("done", tag, ok, failures))

    @staticmethod
    def _who(cfg: AlertConfig, chat_id: str) -> str:
        for s in cfg.subscribers:
            if s["chat_id"] == chat_id:
                return s["name"] or s["username"] or chat_id
        return chat_id

    def _tell_call_blocked(self, cfg: AlertConfig, username: str,
                           err: str) -> None:
        """A refused call is invisible to the person it was meant for -
        tell them, on the channel that does reach them, what to do."""
        m = re.search(r"add (@\w+)", err)
        if "spam block" in err and m:
            fix = (f"CallMeBot needs you to add {m.group(1)} to your Telegram "
                   f"contacts and send it any message (one time). Details: "
                   f"https://www.callmebot.com/blog/spam-error/")
        else:
            fix = (f"CallMeBot is not allowed to call you yet. Open "
                   f"{CALLMEBOT_BOT} in Telegram and press Start (one time).")
        for s in cfg.subscribers:
            if s.get("username") == username and not s.get("group"):
                try:
                    self._telegram(cfg, s["chat_id"],
                                   f"📞 FD Studio tried to CALL you about "
                                   f"{cfg.name}, but it did not go through. "
                                   f"{fix}", silent=False)
                except RuntimeError:
                    pass

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
        low = text.lower()
        if "not authorized" in low or "is not received" in low:
            raise RuntimeError("CallMeBot refused — this person must first "
                               f"send /start to {CALLMEBOT_BOT} in Telegram")
        # Seen 2026-09-30: "Someone reported CallMeBot as spammer, please add
        # @CallMeBot_API16 in your Telegram contacts and send him a message".
        # Also HTTP 200, and it was being counted as a successful call. The
        # account number varies per person (CallMeBot's spam-error page).
        if "as spammer" in low or "spam-error" in low:
            m = re.search(r"@CallMeBot_API\w*", text, re.IGNORECASE)
            caller = m.group(0) if m else "the CallMeBot account that calls you"
            raise RuntimeError(f"CallMeBot refused (spam block) — add {caller} "
                               f"to Telegram contacts and send it a message")
        return f"CallMeBot: {text[-160:]}" if text else ""

    # ── subscriptions: the bot's inbox ───────────────────────────────────────
    def start_listening(self) -> None:
        """Watch the bot for /start and /stop, for the life of the process."""
        threading.Thread(target=self._listen, daemon=True,
                         name="telegram-inbox").start()

    def _listen(self) -> None:
        offset: int | None = None
        last_err = ""
        token_seen = ""
        while True:
            cfg, problem = self.config()
            token = cfg.telegram_bot_token
            if problem or not token:
                time.sleep(5.0)          # set up later; pick it up then
                continue
            api = lambda m: TELEGRAM_API.format(token=token, method=m)  # noqa: E731
            if token != token_seen:
                try:
                    me = json.loads(self._http(api("getMe"))).get("result", {})
                    self.bot_username = "@" + me.get("username", "")
                    token_seen = token
                    self.results.put(("log", f"phone alerts: listening to "
                                             f"{self.bot_username}"))
                except (RuntimeError, ValueError) as exc:
                    self._inbox_error(str(exc), last_err)
                    last_err = str(exc)
                    time.sleep(30.0)
                    continue
            t0 = time.monotonic()
            params = {"timeout": str(POLL_TIMEOUT_S),
                      "allowed_updates": '["message","my_chat_member"]'}
            if offset is not None:
                params["offset"] = str(offset)
            try:
                updates = json.loads(self._http(api("getUpdates"), params)
                                     ).get("result", [])
            except (RuntimeError, ValueError) as exc:
                msg = str(exc)
                self._inbox_error(msg, last_err)
                last_err = msg
                # 409: another program is reading this bot's updates.
                time.sleep(30.0 if msg.startswith(("HTTP 409", "HTTP 401"))
                           else 10.0)
                continue
            last_err = ""
            for upd in updates:
                offset = int(upd.get("update_id", 0)) + 1
                try:
                    self._handle_update(token, upd)
                except Exception as exc:  # noqa: BLE001 - one bad update only
                    self.results.put(("log", f"phone alerts: could not handle "
                                             f"a Telegram update ({exc!r})"))
            if not updates and time.monotonic() - t0 < 1.0:
                time.sleep(1.0)          # a server answering instantly: no spin

    def _inbox_error(self, msg: str, last: str) -> None:
        if msg == last:
            return                       # once per distinct error, not per poll
        hint = (" — another program (or another FD Studio) is using this "
                "bot; only one may" if msg.startswith("HTTP 409") else "")
        self.results.put(("log", f"phone alerts: Telegram inbox error: "
                                 f"{msg}{hint}"))

    def _handle_update(self, token: str, upd: dict) -> None:
        if "my_chat_member" in upd:
            m = upd["my_chat_member"]
            chat = m.get("chat") or {}
            status = (m.get("new_chat_member") or {}).get("status", "")
            if status in ("kicked", "left"):
                # Blocked the bot, or removed it from the group.
                self._unsubscribe(token, chat, reply=False)
            elif chat.get("type") in ("group", "supergroup") \
                    and status in ("member", "administrator"):
                self._subscribe(token, chat, explicit=True)
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if "id" not in chat:
            return
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/stop"):
            self._unsubscribe(token, chat, reply=True)
        else:
            self._subscribe(token, chat, explicit=text.startswith("/start"))

    def _reply(self, token: str, chat_id: str, text: str,
               silent: bool = False) -> None:
        try:
            self._http(TELEGRAM_API.format(token=token, method="sendMessage"),
                       {"chat_id": chat_id, "text": text,
                        "disable_notification": "true" if silent else "false"})
        except RuntimeError as exc:
            self.results.put(("log", f"phone alerts: could not reply to "
                                     f"{chat_id}: {exc}"))

    def _subscribe(self, token: str, chat: dict, explicit: bool) -> None:
        group = chat.get("type") in ("group", "supergroup", "channel")
        entry = {
            "chat_id": str(chat["id"]),
            "name": (chat.get("title") or " ".join(filter(None, [
                chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username") or "?"),
            "username": ("@" + chat["username"]) if chat.get("username")
            and not group else "",
            "group": group,
        }
        with self._file_lock:
            raw = self._raw_config()
            subs = raw.setdefault("subscribers", [])
            old = next((s for s in subs
                        if str(s.get("chat_id")) == entry["chat_id"]), None)
            if old is None:
                subs.append(entry)
            else:
                old.update(entry)
            _write_json(self.config_path, raw)
            wearer = str(raw.get("wearer_name", "")).strip() or "the wearer"
            others = [str(s["chat_id"]) for s in subs
                      if str(s.get("chat_id")) != entry["chat_id"]]

        if old is None:
            self.results.put(("log", f"phone alerts: {entry['name']} "
                                     f"subscribed"))
            self._reply(token, entry["chat_id"], self._welcome(entry, wearer))
            # Visible, not hidden: anyone can find a bot and press Start.
            for other in others:
                self._reply(token, other, f"👤 {entry['name']} joined the "
                            f"alert list for {wearer}.", silent=True)
        elif explicit:
            self._reply(token, entry["chat_id"],
                        "✅ You're already on the alert list for "
                        f"{wearer}.\n\n" + self._call_note(entry))

    def _unsubscribe(self, token: str, chat: dict, reply: bool) -> None:
        chat_id = str(chat.get("id", ""))
        with self._file_lock:
            raw = self._raw_config()
            subs = raw.get("subscribers", [])
            gone = [s for s in subs if str(s.get("chat_id")) == chat_id]
            raw["subscribers"] = [s for s in subs
                                  if str(s.get("chat_id")) != chat_id]
            if gone:
                _write_json(self.config_path, raw)
            wearer = str(raw.get("wearer_name", "")).strip() or "the wearer"
        if gone:
            self.results.put(("log", f"phone alerts: {gone[0].get('name')} "
                                     f"unsubscribed"))
        if reply:
            self._reply(token, chat_id, f"You've left the alert list for "
                        f"{wearer}. Send /start to join again.")

    def _raw_config(self) -> dict:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else _template()
        except (OSError, ValueError):
            return _template()

    @staticmethod
    def _call_note(entry: dict) -> str:
        if entry["group"]:
            return ("📞 For a phone CALL as well, each person messages this bot "
                    f"privately and presses Start, then presses Start on "
                    f"{CALLMEBOT_BOT}.")
        if not entry["username"]:
            return ("📞 Phone CALLS need a Telegram username, and you don't "
                    "have one. Set it in Telegram → Settings → Username, then "
                    f"send /start here again and press Start on "
                    f"{CALLMEBOT_BOT}.")
        return (f"📞 To also get a PHONE CALL: open {CALLMEBOT_BOT} and press "
                f"Start (one time only).")

    def _welcome(self, entry: dict, wearer: str) -> str:
        who = "This group is" if entry["group"] else "You're"
        return (f"✅ {who} now on the alert list for {wearer}.\n\n"
                f"You'll get a message here if {wearer} may have fallen and "
                f"doesn't respond within 30 seconds, or presses the SOS "
                f"button.\n\n{self._call_note(entry)}\n\n"
                f"Send /stop to leave the list.")

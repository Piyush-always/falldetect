"""
Phone alerts: a Telegram message, plus a loud ntfy push.

WHO GETS THEM
-------------
Telegram: anyone who opens the bot and presses Start. FD Studio listens to
the bot while it runs: /start (or any first message) adds that chat to the
alert list and replies with a welcome, /stop removes it. Adding the bot to a
family group subscribes the group. Nobody edits a file by hand.

ntfy: anyone subscribed to the topic in the free ntfy app. The bot's welcome
message gives each new subscriber the topic and how to add it.

Anyone who finds the bot can subscribe - bot names are searchable - so every
new Telegram subscriber is announced (silently) to everyone already on the
list. A stranger joining is then seen, not hidden. The ntfy topic is a secret
in itself: anyone who has it can read the alerts, so it is only handed out to
people who subscribed to the bot.

WHY THESE TWO
-------------
Free, instant, no business verification. The Telegram Bot API is official and
reliable, but a message is a normal notification that can be muted - it is
the channel of record. ntfy's urgent priority is the loud one: long
vibration bursts and a pop-over, and on Android it can be set to override Do
Not Disturb. It replaced CallMeBot voice calls (2026-09-30): those broke when
another user's spam report blocked the shared caller, whose fix then asked
for 950 Telegram Stars per message.

WHAT THIS IS NOT
----------------
Sent from the laptop, over the laptop's internet. Laptop asleep, offline, or
FD Studio closed means nothing is sent - and new Telegram subscribers are not
picked up either. Every send reports success or failure back to the screen; a
failure is shown as "NOT sent", never swallowed. "Reached" means the service
accepted it: ntfy cannot say how many phones are subscribed to a topic.
Only ONE running FD Studio may use a bot: Telegram hands each update to one
listener, so two would split the subscribers between them.

Deliberately no Qt import (same rule as engine.py): sends and the listener
run on plain threads and report through a queue the GUI drains on its timer.
Standard library only, so the packaged exe gains no dependency.

SECRETS
-------
The bot token and the ntfy topic live in %USERPROFILE%\\.fd_studio\\
alerts.json, and the people who pressed Start in subscribers.json next to it
(copy BOTH when moving to another laptop) - outside the repo, so they cannot
be committed (and not in AppData: see CONFIG_PATH). A shipped exe can carry them
(tools/build_exe.ps1 bundles alerts.bundle.json, which is git-ignored); on
first run it seeds the settings file from that. Such an exe contains the
secrets: share it privately, never commit or publish it.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
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

#: Name of the settings file build_exe.ps1 bundles into a shipped exe.
BUNDLE_NAME = "alerts.bundle.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
NTFY_DEFAULT_SERVER = "https://ntfy.sh"

#: ntfy priorities: 5 = urgent (long vibration bursts, pop-over), 3 = default,
#: 2 = low (no sound or vibration).
NTFY_URGENT, NTFY_NORMAL, NTFY_QUIET = 5, 3, 2

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
        "ntfy_topic: a long random name, e.g. fd-sos-3f9a1c7e2b6d4a80. Keep it "
        "private: anyone who has it can read the alerts.",
        "telegram_chat_ids: optional extra Telegram chats, added by hand, as "
        "a list: [\"123456789\"].",
        "People who press Start on the bot are kept in subscribers.json next "
        "to this file - managed by the bot, not edited here.",
        "Free services, no guarantee. Alerts go out only while this laptop "
        "is on, online, and FD Studio is open.",
    ],
    "wearer_name": "",
    "telegram_bot_token": "",
    "ntfy_topic": "",
    "ntfy_server": NTFY_DEFAULT_SERVER,
    "telegram_chat_ids": [],
}

#: Settings a shipped exe may carry in its bundle, filled in only if missing.
_BUNDLE_FIELDS = ("telegram_bot_token", "wearer_name", "ntfy_topic",
                  "ntfy_server")

#: Bot-managed subscriber list, beside alerts.json. Its own file because
#: alerts.json is the one opened in Notepad: a save from a Notepad window
#: left open would otherwise write back an old list and silently drop people
#: who had been told they are on it (PR #1 review).
SUBSCRIBERS_NAME = "subscribers.json"


def _template() -> dict:
    """A fresh copy. dict(_TEMPLATE) would share its lists."""
    return json.loads(json.dumps(_TEMPLATE))


def subscribers_path(config_path: Path) -> Path:
    return config_path.with_name(SUBSCRIBERS_NAME)


@dataclass
class AlertConfig:
    wearer_name: str = ""
    telegram_bot_token: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = NTFY_DEFAULT_SERVER
    #: [{"chat_id", "name", "username" ("@x" or ""), "group": bool}]
    subscribers: list[dict] = field(default_factory=list)
    telegram_chat_ids: list[str] = field(default_factory=list)
    #: True when some recipients could not be read (subscribers.json or
    #: telegram_chat_ids broken): a send then reaches only some of them and
    #: must not be reported as complete.
    recipients_incomplete: bool = False

    @property
    def name(self) -> str:
        return self.wearer_name.strip() or "the wearer"

    @property
    def message_targets(self) -> list[str]:
        ids = [s["chat_id"] for s in self.subscribers] + self.telegram_chat_ids
        return list(dict.fromkeys(ids))

    @property
    def configured(self) -> bool:
        return bool(self.telegram_bot_token and self.message_targets) \
            or bool(self.ntfy_topic)


class _Bad(Exception):
    """A settings value of the wrong type - reported, never raised on."""


def _as_str(value, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    raise _Bad(f"{name} must be text")


def _as_list(value, name: str) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return [value]          # one chat id typed without brackets
    raise _Bad(f"{name} must be a list")


def _clean_subscribers(items) -> list[dict]:
    out = []
    for s in _as_list(items, "subscribers"):
        if isinstance(s, dict) and _as_str(s.get("chat_id"), "chat_id"):
            out.append({"chat_id": _as_str(s.get("chat_id"), "chat_id"),
                        "name": _as_str(s.get("name"), "name"),
                        "username": _as_str(s.get("username"), "username"),
                        "group": bool(s.get("group", False))})
    return out


def read_subscribers(config_path: Path) -> tuple[list[dict], str]:
    """(subscribers, problem). Missing file = nobody yet. An unreadable file
    is a PROBLEM, not an empty list: treating it as empty and writing back
    would wipe everyone (PR #1 review)."""
    path = subscribers_path(config_path)
    if not path.exists():
        return [], ""
    try:
        return _clean_subscribers(json.loads(path.read_text(encoding="utf-8"))), ""
    except (OSError, ValueError, _Bad) as exc:
        return [], f"cannot read {path.name}: {exc}"


def load_config(path: Path = CONFIG_PATH) -> tuple[AlertConfig, str]:
    """(config, problem). A broken file is reported, never guessed at - and
    never raised on: this runs inside the alert path (PR #1 review)."""
    if not path.exists():
        return AlertConfig(), ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return AlertConfig(), f"cannot read {path.name}: {exc}"
    if not isinstance(raw, dict):
        return AlertConfig(), f"{path.name} is not a JSON object"
    problems: list[str] = []
    cfg = AlertConfig()

    def field_(fn, *args):
        """One field at a time: a bad optional field is skipped and
        reported, never allowed to block every alert (PR #1 re-check)."""
        try:
            return fn(*args), True
        except _Bad as exc:
            problems.append(f"{path.name}: {exc}")
            return None, False

    for key in ("wearer_name", "telegram_bot_token", "ntfy_topic"):
        value, ok = field_(_as_str, raw.get(key), key)
        if ok:
            setattr(cfg, key, value)
    server, ok = field_(_as_str, raw.get("ntfy_server"), "ntfy_server")
    if ok and server:
        cfg.ntfy_server = server.rstrip("/")
    ids, ok = field_(lambda v: [c for c in (_as_str(x, "telegram_chat_ids")
                                            for x in _as_list(v, "telegram_chat_ids"))
                                if c], raw.get("telegram_chat_ids"))
    if ok:
        cfg.telegram_chat_ids = ids
    else:
        cfg.recipients_incomplete = True

    subs, sub_problem = read_subscribers(path)
    if sub_problem:
        problems.append(sub_problem)
        cfg.recipients_incomplete = True
    if not subscribers_path(path).exists():
        # Only BEFORE migration: an old alerts.json still holding the list.
        # Once subscribers.json exists it is the only list - a stale
        # Notepad buffer that writes the old list back must not bring back
        # people who sent /stop (PR #1 re-check).
        legacy, ok = field_(_clean_subscribers, raw.get("subscribers"))
        if ok:
            subs = legacy
    cfg.subscribers = subs
    return cfg, "; ".join(problems)


def _write_json(path: Path, raw) -> None:
    """Write via a temp file and rename: a crash mid-write must not leave a
    half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def migrate_subscribers(path: Path = CONFIG_PATH) -> int:
    """Move subscribers from an older alerts.json into subscribers.json.
    Run at startup, before anyone can have Notepad open. Returns how many."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        legacy = _clean_subscribers(raw.get("subscribers")) \
            if isinstance(raw, dict) else []
    except (OSError, ValueError, _Bad):
        return 0
    if not legacy or subscribers_path(path).exists():
        return 0                 # nothing to move, or already moved
    try:
        _write_json(subscribers_path(path), legacy)
    except OSError:
        return 0                 # load_config keeps honouring the old list
    try:
        raw.pop("subscribers", None)
        _write_json(path, raw)
    except OSError:
        pass                     # e.g. read-only: the list is ignored now
    return len(legacy)


def _bundled_defaults() -> dict:
    """Settings baked into a shipped exe, or {}."""
    base = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not base:
        return {}
    try:
        raw = json.loads((Path(base) / BUNDLE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def seed_from_bundle(path: Path = CONFIG_PATH) -> bool:
    """A shipped exe: fill any MISSING setting from the bundle, so the person
    receiving it never opens a settings file. Never overwrites a value that
    is already set, and never touches subscribers."""
    bundle = _bundled_defaults()
    if not bundle:
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() \
            else _template()
    except (OSError, ValueError):
        return False            # a broken file is reported by load_config
    if not isinstance(raw, dict):
        return False
    changed = False
    for key in _BUNDLE_FIELDS:
        value = str(bundle.get(key, "")).strip()
        current = str(raw.get(key, "") or "").strip()
        if value and (not current or (key == "ntfy_server"
                                      and current == NTFY_DEFAULT_SERVER
                                      and value != current)):
            raw[key] = value
            changed = True
    if changed:
        _write_json(path, raw)
    return changed


def ensure_config_file(path: Path = CONFIG_PATH) -> Path:
    """Create the template if missing, so 'open settings' has something
    to open. Never overwrites an existing file."""
    if not path.exists():
        _write_json(path, _template())
    return path


def _http(url: str, data: dict | None = None,
          json_body: dict | None = None) -> bytes:
    """GET (no body), form POST (data) or JSON POST (json_body). Raises ONLY
    RuntimeError, with the service's own error text where there is one.

    Never puts the URL in the message: a Telegram URL contains the bot token,
    and these messages go to the Log tab (PR #1 review).
    """
    headers = {"User-Agent": "FD-Studio"}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read()[:300].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - connection died mid-error-body
            detail = ""
        try:
            j = json.loads(detail)
            detail = (j.get("description") or j.get("error") or detail) \
                if isinstance(j, dict) else detail
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"no connection ({reason})") from None
    except (ValueError, http.client.HTTPException) as exc:
        # A bad URL (e.g. a space in the token) or a garbled reply.
        raise RuntimeError(f"bad request or reply ({type(exc).__name__})") \
            from None


def _reply_json(raw: bytes, service: str) -> dict:
    """A service's JSON reply as a dict. A 200 with HTML (a proxy or captive
    portal page) is a failure, not a crash (PR #1 review)."""
    try:
        j = json.loads(raw or b"{}")
    except ValueError:
        raise RuntimeError(f"unexpected reply from {service} (not JSON)") \
            from None
    if not isinstance(j, dict):
        raise RuntimeError(f"unexpected reply from {service}")
    return j


def ntfy_host(cfg: AlertConfig) -> str:
    return cfg.ntfy_server.split("://", 1)[-1]


class Notifier:
    """Fire-and-report phone alerts, plus the bot's subscription inbox.
    Safe to call from the GUI thread.

    results carries ("log", text) and ("done", tag, ok, failures,
    telegram_ok, ntfy_ok, complete) tuples: ok is the number of deliveries
    the services accepted, failures a list of "who: why" strings, complete
    False when some recipients could not be read. "Accepted" is all that can be known - a message can
    sit unread, and ntfy cannot say who is subscribed.
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
        parts = []
        if cfg.telegram_bot_token:
            names = [s["name"] or s["username"] or s["chat_id"]
                     for s in cfg.subscribers]
            if cfg.message_targets:
                shown = ", ".join(names[:3]) + (f" +{len(names) - 3}"
                                                 if len(names) > 3 else "")
                parts.append(f"{len(cfg.message_targets)} on Telegram"
                             + (f" ({shown})" if shown else ""))
            else:
                parts.append("nobody on Telegram yet")
        if cfg.ntfy_topic:
            parts.append(f"loud alarm on ntfy topic {cfg.ntfy_topic}")
        line = "Phone alerts: " + " · ".join(parts)
        if self.share_link:
            line += (f"\nTo subscribe: open {self.share_link} in Telegram and "
                     f"press Start.")
        return line

    # ── sending ──────────────────────────────────────────────────────────────
    def send(self, tag: str, title: str, body: str, urgent: bool = False,
             silent: bool = False) -> bool:
        """Start sending. False (and nothing sent) if not set up.

        urgent: the ntfy push goes out at priority 5 (long vibration bursts).
        silent: Telegram without a sound, ntfy at low priority - for notes
        nobody should be woken for. Neither: a normal notification.
        """
        cfg, problem = self.config()
        if not cfg.configured:
            return False
        if problem:     # e.g. subscribers.json unreadable: send to the rest
            self.results.put(("log", f"phone alerts: {problem}"))
        self._jobs.put(lambda: self._send_all(cfg, tag, title, body,
                                              urgent, silent))
        return True

    def send_test(self) -> None:
        """Queue an urgent test to everyone. Reports as "test"."""
        self._jobs.put(self._test_job)

    def _test_job(self) -> None:
        cfg, problem = self.config()
        if problem and not cfg.configured:
            self.results.put(("done", "test", 0, [problem], 0, 0, False))
            return
        if not cfg.configured:
            how = (f"send people the link {self.share_link} and ask them to "
                   f"press Start" if self.share_link else
                   "press 'Phone alert settings' and add the bot token")
            self.results.put(("done", "test", 0,
                              [f"nobody to alert yet — {how}"], 0, 0, False))
            return
        # Urgent on purpose: a test has to prove the LOUD alarm works.
        self._send_all(cfg, "test", "🧪 Test from FD Studio",
                       f"Phone alerts for {cfg.name} are working.",
                       urgent=True, silent=False)

    def _send_all(self, cfg: AlertConfig, tag: str, title: str, body: str,
                  urgent: bool, silent: bool) -> None:
        # ntfy FIRST: it is the loud one (can override Do Not Disturb), and
        # queued after every Telegram chat's retries it could arrive minutes
        # late when Telegram is unreachable (PR #1 review).
        jobs = []
        if cfg.ntfy_topic:
            prio = NTFY_URGENT if urgent else (NTFY_QUIET if silent
                                               else NTFY_NORMAL)
            jobs.append(("ntfy", lambda: self._ntfy(cfg, title, body, prio,
                                                    urgent)))
        if cfg.telegram_bot_token:
            for chat in cfg.message_targets:
                jobs.append((f"Telegram {self._who(cfg, chat)}",
                             lambda c=chat: self._telegram(
                                 cfg, c, f"{title}\n{body}", silent)))

        # ("done", tag, ok, failures, telegram_ok, ntfy_ok, complete).
        # Counted per channel because only Telegram can confirm a person:
        # ntfy accepts a post whether or not any phone is subscribed (PR #1
        # review). complete is False when some recipients could not even be
        # read, so a partial send is never shown as "family alerted".
        ok = tg_ok = ntfy_ok = 0
        failures: list[str] = []
        if cfg.recipients_incomplete:
            failures.append("some recipients: the subscriber list could not "
                            "be read (see Log)")
        try:
            for who, job in jobs:
                err = self._with_retries(job)
                if err is None:
                    ok += 1
                    if who == "ntfy":
                        ntfy_ok += 1
                    else:
                        tg_ok += 1
                    self.results.put(("log", f"phone alert [{tag}]: {who} — accepted"))
                else:
                    failures.append(f"{who}: {err}")
                    self.results.put(("log", f"phone alert [{tag}]: {who} — FAILED: {err}"))
        finally:
            # Always an outcome, or the screen says "Sending..." for ever.
            self.results.put(("done", tag, ok, failures, tg_ok, ntfy_ok,
                              not cfg.recipients_incomplete))

    @staticmethod
    def _who(cfg: AlertConfig, chat_id: str) -> str:
        for s in cfg.subscribers:
            if s["chat_id"] == chat_id:
                return s["name"] or s["username"] or chat_id
        return chat_id

    def _with_retries(self, job) -> str | None:
        """None on success, else the last error."""
        last = ""
        for delay in (0.0,) + RETRY_DELAYS_S:
            if delay:
                time.sleep(delay)
            try:
                job()
                return None
            except RuntimeError as exc:
                last = str(exc)
                # A wrong token, unknown chat or bad topic will not fix
                # itself by retrying. (429 - rate limited - might.)
                if last.startswith(("HTTP 400", "HTTP 401", "HTTP 403",
                                    "HTTP 404")):
                    break
            except Exception as exc:  # noqa: BLE001 - never abort the batch
                # Type only: str() of some errors carries the URL, and a
                # Telegram URL carries the bot token.
                return f"unexpected error ({type(exc).__name__})"
        return last

    def _telegram(self, cfg: AlertConfig, chat: str, text: str,
                  silent: bool) -> None:
        raw = self._http(
            TELEGRAM_API.format(token=cfg.telegram_bot_token, method="sendMessage"),
            {"chat_id": chat, "text": text,
             "disable_notification": "true" if silent else "false"})
        if not _reply_json(raw, "Telegram").get("ok"):
            raise RuntimeError(f"Telegram refused: {raw[:200]!r}")

    def _ntfy(self, cfg: AlertConfig, title: str, body: str, priority: int,
              urgent: bool) -> None:
        # JSON, not headers: the titles carry emoji, which HTTP headers do
        # not reliably survive (ntfy's own publishing docs).
        raw = self._http(cfg.ntfy_server + "/", json_body={
            "topic": cfg.ntfy_topic, "title": title, "message": body,
            "priority": priority,
            "tags": ["rotating_light"] if urgent else [],
        })
        if not _reply_json(raw, "ntfy").get("id"):
            raise RuntimeError(f"ntfy refused: {raw[:200]!r}")

    # ── subscriptions: the bot's inbox ───────────────────────────────────────
    def start_listening(self) -> None:
        """Watch the bot for /start and /stop, for the life of the process."""
        threading.Thread(target=self._listen, daemon=True,
                         name="telegram-inbox").start()

    def _listen(self) -> None:
        """Never returns. A dead inbox is silent - nobody new is subscribed
        while the QR code keeps sending people to the bot - so every error is
        logged once and the loop carries on (PR #1 review)."""
        offset: int | None = None
        last_err = ""
        token_seen = ""
        while True:
            try:
                cfg, _problem = self.config()
                token = cfg.telegram_bot_token
                if not token:
                    time.sleep(5.0)      # set up later; pick it up then
                    continue
                api = lambda m: TELEGRAM_API.format(token=token, method=m)  # noqa: E731
                if token != token_seen:
                    # A different bot: its update ids have nothing to do with
                    # the old one's, and a stale offset would confirm (discard)
                    # its /start messages unseen (PR #1 review).
                    offset = None
                    self.bot_username = ""
                    me = _reply_json(self._http(api("getMe")), "Telegram")
                    self.bot_username = "@" + str((me.get("result") or {})
                                                  .get("username", ""))
                    token_seen = token
                    self.results.put(("log", f"phone alerts: listening to "
                                             f"{self.bot_username}"))
                t0 = time.monotonic()
                params = {"timeout": str(POLL_TIMEOUT_S),
                          "allowed_updates": '["message","my_chat_member"]'}
                if offset is not None:
                    params["offset"] = str(offset)
                updates = _reply_json(self._http(api("getUpdates"), params),
                                      "Telegram").get("result") or []
                last_err = ""
                for upd in updates:
                    offset = int(upd.get("update_id", 0)) + 1
                    try:
                        self._handle_update(token, upd)
                    except Exception as exc:  # noqa: BLE001 - one bad update only
                        self.results.put(("log", f"phone alerts: could not "
                                                 f"handle a Telegram update "
                                                 f"({type(exc).__name__})"))
                if not updates and time.monotonic() - t0 < 1.0:
                    time.sleep(1.0)      # a server answering instantly: no spin
            except Exception as exc:  # noqa: BLE001 - the inbox must not die
                msg = str(exc) if isinstance(exc, RuntimeError) \
                    else f"unexpected error ({type(exc).__name__})"
                self._inbox_error(msg, last_err)
                last_err = msg
                # 409: another program is reading this bot's updates.
                time.sleep(30.0 if msg.startswith(("HTTP 409", "HTTP 401"))
                           else 10.0)

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
        except Exception as exc:  # noqa: BLE001 - after a subscribe write
            msg = str(exc) if isinstance(exc, RuntimeError) \
                else type(exc).__name__
            self.results.put(("log", f"phone alerts: could not reply to "
                                     f"{chat_id}: {msg}"))

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
            subs, problem = read_subscribers(self.config_path)
            if problem:
                # Never write over a file we could not read: that would wipe
                # everyone on it (PR #1 review). Say so instead.
                self.results.put(("log", f"phone alerts: NOT adding "
                                         f"{entry['name']} — {problem}"))
                self._reply(token, entry["chat_id"],
                            "Sorry — FD Studio could not add you just now. "
                            "Please send /start again in a few minutes.")
                return
            old = next((x for x in subs if x["chat_id"] == entry["chat_id"]),
                       None)
            if old is None:
                subs.append(entry)
            else:
                old.update(entry)
            _write_json(subscribers_path(self.config_path), subs)
            others = [x["chat_id"] for x in subs
                      if x["chat_id"] != entry["chat_id"]]
        cfg, _ = load_config(self.config_path)

        if old is None:
            self.results.put(("log", f"phone alerts: {entry['name']} "
                                     f"subscribed"))
            self._reply(token, entry["chat_id"], self._welcome(entry, cfg))
            # Visible, not hidden: anyone can find a bot and press Start.
            for other in others:
                self._reply(token, other, f"👤 {entry['name']} joined the "
                            f"alert list for {cfg.name}.", silent=True)
        elif explicit:
            self._reply(token, entry["chat_id"],
                        f"✅ You're already on the alert list for {cfg.name}."
                        + self._ntfy_note(cfg))

    def _unsubscribe(self, token: str, chat: dict, reply: bool) -> None:
        chat_id = str(chat.get("id", ""))
        with self._file_lock:
            subs, problem = read_subscribers(self.config_path)
            if problem:
                self.results.put(("log", f"phone alerts: could not remove "
                                         f"{chat_id} — {problem}"))
                if reply:
                    self._reply(token, chat_id, "Sorry — FD Studio could not "
                                "remove you just now. Please send /stop again "
                                "in a few minutes.")
                return
            gone = [x for x in subs if x["chat_id"] == chat_id]
            if gone:
                _write_json(subscribers_path(self.config_path),
                            [x for x in subs if x["chat_id"] != chat_id])
        cfg, _ = load_config(self.config_path)
        if gone:
            self.results.put(("log", f"phone alerts: {gone[0]['name']} "
                                     f"unsubscribed"))
        if reply:
            self._reply(token, chat_id, f"You've left the alert list for "
                        f"{cfg.name}. Send /start to join again. If you added "
                        f"the ntfy topic, remove it in the ntfy app too.")

    @staticmethod
    def _ntfy_note(cfg: AlertConfig) -> str:
        """How to get the loud alarm - only to people on the bot, since the
        topic itself is what grants access."""
        if not cfg.ntfy_topic:
            return ""
        host = ntfy_host(cfg)
        server = ("" if cfg.ntfy_server == NTFY_DEFAULT_SERVER else
                  f"   (server: {cfg.ntfy_server} - set it in the ntfy app "
                  f"under 'Use another server')\n")
        return (f"\n\n🔔 For a LOUD alarm on your phone (long vibration, even "
                f"when Telegram is muted):\n"
                f"1. Install the free app \"ntfy\" (Play Store / App Store).\n"
                f"2. Open it, tap +, and subscribe to this topic:\n"
                f"   {cfg.ntfy_topic}\n{server}"
                f"   Android shortcut: ntfy://{host}/{cfg.ntfy_topic}\n"
                f"3. Android: in ntfy, allow 'Urgent' notifications to "
                f"override Do Not Disturb.\n"
                f"Keep the topic private - anyone who has it can read these "
                f"alerts.")

    def _welcome(self, entry: dict, cfg: AlertConfig) -> str:
        who = "This group is" if entry["group"] else "You're"
        return (f"✅ {who} now on the alert list for {cfg.name}.\n\n"
                f"You'll get a message here if {cfg.name} may have fallen and "
                f"doesn't respond within 30 seconds, or presses the SOS "
                f"button.{self._ntfy_note(cfg)}\n\n"
                f"Send /stop to leave the list.")

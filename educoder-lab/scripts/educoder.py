#!/usr/bin/env python3
"""EduCoder (头歌) lab automation CLI.

Talks to the platform JSON API directly: no browser, no page snapshots,
no third-party dependencies. Credentials are cached locally after first login.

Commands:
    login      authenticate and cache the session
    status     show cached session validity
    courses    list my courses
    homeworks  list homework tasks in a course
    task       show one challenge: statement, tests, paths, next/prev game
    code       read remote answer file content
    submit     save code remotely (one file, or --dir) and grade it once
    evaluate   re-run grading for the current commit, without re-uploading
    auto       walk unfinished challenges of one homework and report
    stats      show how often this skill was used (local counters only)
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://data.educoder.net/api"
AK = "e9dd5b4322f9f7d83d009de9bfa100c3"
SK = "2e3da06ae26ba9f76a5d8d355746f2fe"
CONFIG_DIR = Path(os.environ.get("EDUCODER_HOME") or (Path.home() / ".educoder"))
SESSION_FILE = CONFIG_DIR / "session.json"
CACHE_DIR = CONFIG_DIR / "cache"
USAGE_FILE = CONFIG_DIR / "usage.json"
CACHE_TTL = 600.0          # seconds; bypass with --refresh
MAX_OUTPUT_DEFAULT = 2000  # chars, hard cap on anything printed into the transcript

SCRIPT_VERSION = "1.2.0"
SKILL_NAME = "educoder-lab"

# Local usage counters. They never leave this machine: no endpoint is contacted,
# and only the command name, a timestamp and the outcome are stored.
USAGE_DAYS_KEPT = 30
USAGE_DISABLED_ENV = "EDUCODER_NO_USAGE"
DEFAULT_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"

# Transient HTTP failures worth retrying (429 gets a longer backoff).
RETRIES = 3
BACKOFF = 0.5
RETRY_STATUS = {429, 500, 502, 503, 504}

# keep Chinese output intact on Windows consoles; reconfigure lives on
# TextIOWrapper, not on the TextIO protocol, hence the getattr guard.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        with contextlib.suppress(Exception):
            _reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def gen_signature(method: str) -> tuple[int, str]:
    try:
        ts = int(time.time() * 1000)
    except (TypeError, ValueError, OverflowError):
        ts = 0
    params = f"method={method.upper()}&ak={AK}&sk={SK}&time={ts}"
    b64 = base64.b64encode(params.encode()).decode()
    # The platform mandates md5 over a base64 payload; this is a wire-protocol
    # signature, not a security primitive, so usedforsecurity=False is correct.
    # The algorithm is not a choice: the server rejects any other digest with
    # status=-102, so this is a deliberate, reviewed exception rather than a fix.
    # pi-lens-ignore: python-weak-hash
    return ts, hashlib.md5(b64.encode(), usedforsecurity=False).hexdigest()  # noqa: S324


class ApiError(RuntimeError):
    pass


def _retry_pause(status: int, attempt: int) -> float | None:
    """Seconds to wait before retrying this response, or None if it is final.

    Kept as a helper so the except block stays free of nested conditionals.
    """
    if status not in RETRY_STATUS:
        return None
    if attempt >= RETRIES - 1:
        return None
    # 429 means rate limited: back off much harder than a transient 5xx.
    factor = 4 if status == 429 else 1
    return BACKOFF * (2 ** attempt) * factor


class SessionExpired(ApiError):
    pass


class Api:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.zzud = ""
        self.session = ""
        self.autologin = ""

    # -- cookies ----------------------------------------------------------
    def _cookie_header(self) -> str:
        parts = []
        if self.autologin:
            parts.append(f"autologin_trustie={self.autologin}")
        if self.session:
            parts.append(f"_educoder_session={self.session}")
        return "; ".join(parts)

    def load(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.zzud = str(data.get("zzud") or "")
        self.session = str(data.get("session") or "")
        self.autologin = str(data.get("autologin") or "")
        return bool(self.zzud and self.session)

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"zzud": self.zzud, "session": self.session, "autologin": self.autologin},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):
            # best-effort hardening; the file is still written on platforms
            # without chmod
            os.chmod(tmp, 0o600)
        tmp.replace(SESSION_FILE)

    # -- http -------------------------------------------------------------
    def request(self, method: str, path: str, body=None, *, pc_auth=None,
                extra_headers=None):
        ts, sig = gen_signature(method)
        headers = {
            "X-EDU-Type": "pc",
            "X-EDU-Timestamp": str(ts),
            "X-EDU-Signature": sig,
            "Pc-Authorization": self.session if pc_auth is None else pc_auth,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        ck = self._cookie_header()
        if ck:
            headers["Cookie"] = ck
        if extra_headers:
            headers.update(extra_headers)
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"

        if not path.startswith("/"):
            raise ApiError(f"refusing non-relative API path: {path!r}")
        url = BASE + path
        # BASE is a hard-coded https origin; refuse anything else so a
        # malformed path can never redirect the request to file:/ or another
        # scheme.
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ApiError(f"refusing non-https URL: {url[:80]!r}")
        # The scheme is allowlisted above (https only); this open can never
        # reach file:/ or a custom scheme, so S310 is a false positive here.
        req = urllib.request.Request(  # noqa: S310
            url, data=payload, headers=headers, method=method)
        raw = ""
        for attempt in range(RETRIES):
            try:
                with self.opener.open(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 401:
                    raise SessionExpired("session expired (HTTP 401)") from None
                pause = _retry_pause(exc.code, attempt)
                if pause is None:
                    raise ApiError(f"HTTP {exc.code}: {raw[:400]}") from None
                time.sleep(pause)
                continue
            except urllib.error.URLError as exc:
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF * (2 ** attempt))
                    continue
                raise ApiError(
                    f"network error after {RETRIES} tries: {exc.reason}") from None

        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(f"non-JSON response: {raw[:400]}") from None
        if not isinstance(data, dict):
            raise ApiError("response is not a JSON object")
        if data.get("status") == -102:
            raise ApiError("signature rejected (status=-102); AK/SK or clock skew")
        return data

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body):
        return self.request("POST", path, body)

    # -- auth -------------------------------------------------------------
    def login(self, login: str, password: str) -> dict:
        data = self.request(
            "POST",
            "/accounts/login.json",
            {"login": login, "password": password, "autologin": True,
             "tl": None, "source": None},
            pc_auth="null",
            extra_headers={"Origin": "https://www.educoder.net",
                           "Referer": "https://www.educoder.net/login"},
        )
        status = data.get("status")
        if status == -3:
            raise ApiError("用户名或密码错误")
        if status == -4:
            raise ApiError("账号需要绑定手机号或邮箱")
        if isinstance(status, int) and status < 0:
            raise ApiError(f"登录失败 (status={status}): {data.get('message')}")

        for c in self.jar:
            if c.name == "_educoder_session":
                self.session = c.value
            elif c.name == "autologin_trustie":
                self.autologin = c.value
        if not self.session:
            raise ApiError("登录响应缺少 _educoder_session cookie")
        self.zzud = str(data.get("login") or login)
        self.save()
        return data

    def ensure(self) -> None:
        if not (self.zzud and self.session):
            self.load()
        if not (self.zzud and self.session):
            raise ApiError("未登录：请先运行 `educoder.py login`")


# --------------------------------------------------------------------------
# selectors: accept course identifier, numeric id, or name substring
# --------------------------------------------------------------------------
def pick(items, query, *keys, label="item"):
    q = str(query).strip()
    for it in items:
        for k in keys:
            if str(it.get(k)) == q:
                return it
    ql = q.lower()
    hits = [it for it in items
            if any(ql in str(it.get(k, "")).lower() for k in keys)]
    if not hits:
        raise ApiError(f"没有匹配 {label}: {query!r}")
    if len(hits) > 1:
        names = ", ".join(str(h.get("name") or h.get("homework_id") or h.get("id"))
                          for h in hits[:8])
        raise ApiError(f"{label} {query!r} 匹配到 {len(hits)} 个: {names}")
    return hits[0]


def fetch_courses(api: Api, *, refresh: bool = False) -> list[dict]:
    key = f"courses-{api.zzud}"
    if not refresh:
        cached = cache_read(key)
        if isinstance(cached, list):
            return cached
    data = api.get(f"/courses.json?page=1&limit=100&order=mine&search=&zzud={api.zzud}")
    items = data.get("courses") or []
    cache_write(key, items)
    return items


def resolve_course(api: Api, query: str, *, refresh: bool = False) -> dict:
    return pick(fetch_courses(api, refresh=refresh), query,
                "identifier", "id", "name", label="课程")


def fetch_homeworks(api: Api, cid: str, *, refresh: bool = False) -> list[dict]:
    key = f"homeworks-{cid}"
    if not refresh:
        cached = cache_read(key)
        if isinstance(cached, list):
            return cached
    data = api.get(
        f"/courses/{cid}/homework_commons.json"
        f"?limit=100&status=0&id={cid}&type=4&order=0&zzud={api.zzud}"
    )
    items = data.get("homeworks") or []
    cache_write(key, items)
    return items


def resolve_homework(api: Api, course: dict, query: str, *,
                     refresh: bool = False) -> dict:
    return pick(fetch_homeworks(api, course["identifier"], refresh=refresh), query,
                "homework_id", "name", "shixun_identifier", label="作业")


def task_detail(api: Api, game_identifier: str, homework_common_id: int | None = None) -> dict:
    query = f"zzud={api.zzud}"
    if homework_common_id:
        query = f"homework_common_id={homework_common_id}&{query}"
    return api.get(f"/tasks/{game_identifier}.json?{query}")


def split_paths(raw) -> list[str]:
    """challenge.path holds several files separated by full/half-width semicolons."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[；;]", str(raw)) if p.strip()]


def entry_game_id(api: Api, homework: dict) -> str:
    """Resolve the first challenge's game identifier for a homework.

    The homework list only carries shixun/myshixun ids; the real per-challenge
    game identifier comes from shixun_exec.json.
    """
    sid = homework.get("shixun_identifier")
    hw = homework.get("homework_id")
    if sid and hw:
        try:
            data = api.get(f"/shixuns/{sid}/shixun_exec.json"
                           f"?homework_common_id={hw}&zzud={api.zzud}")
        except ApiError:
            data = {}
        gid = data.get("game_identifier")
        if gid:
            return str(gid)
    return str(homework.get("myshixun_identifier") or "")


# --------------------------------------------------------------------------
# output helpers (token-lean by default)
# --------------------------------------------------------------------------
def emit(obj, as_json: bool, text_fn=None, fields=None):
    if fields:
        obj = select_fields(obj, fields)
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif text_fn:
        text_fn(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def strip_html(html: str) -> str:
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def bounded(text: str, limit: int | None = None) -> str:
    """Hard-cap text headed for the transcript: token cost is per character."""
    try:
        cap = MAX_OUTPUT_DEFAULT if limit is None else int(limit)
    except (TypeError, ValueError):
        cap = MAX_OUTPUT_DEFAULT
    if cap <= 0 or len(text) <= cap:
        return text
    return f"{text[:cap]}\n... [truncated {len(text) - cap} of {len(text)} chars]"


def select_fields(obj, fields):
    """Keep only the named top-level keys (recurses into lists)."""
    if not fields:
        return obj
    if isinstance(obj, dict):
        return {k: obj.get(k) for k in fields}
    if isinstance(obj, list):
        return [select_fields(item, fields) for item in obj]
    return obj


# --------------------------------------------------------------------------
# on-disk cache: course/homework lists change slowly, and every fetch costs a
# round trip plus transcript noise
# --------------------------------------------------------------------------
def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"


def cache_read(key: str, ttl: float = CACHE_TTL):
    if ttl <= 0:
        return None
    try:
        blob = json.loads(_cache_path(key).read_text(encoding="utf-8"))
        age = time.time() - float(blob.get("fetched_at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(blob, dict) or age > ttl:
        return None
    return blob.get("data")


def cache_write(key: str, data) -> None:
    with contextlib.suppress(OSError):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8")


# --------------------------------------------------------------------------
# local usage counters (stats)
# --------------------------------------------------------------------------
def _usage_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _as_int(value) -> int:
    """Coerce a stored counter to int; a corrupt value reads as 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_usage_file() -> dict:
    """Load the local counter file; missing or corrupt data reads as empty."""
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_usage(command: str, *, ok: bool = True) -> None:
    """Count one CLI run in `~/.educoder/usage.json`.

    Local by design: nothing is uploaded anywhere, and no account, path or
    argument is stored -- just the command name, a timestamp and the outcome.
    Set EDUCODER_NO_USAGE=1 to switch counting off. Any failure in here is
    swallowed, because statistics must never break the actual work.
    """
    if os.environ.get(USAGE_DISABLED_ENV):
        return
    try:
        data = _read_usage_file()
        now = _usage_now()
        data["total_runs"] = _as_int(data.get("total_runs")) + 1
        commands = data.get("commands")
        commands = commands if isinstance(commands, dict) else {}
        commands[command] = _as_int(commands.get(command)) + 1
        data["commands"] = commands
        if not ok:
            data["failures"] = _as_int(data.get("failures")) + 1
        data["first_used_at"] = data.get("first_used_at") or now
        data["last_used_at"] = now
        by_day = data.get("by_day")
        by_day = by_day if isinstance(by_day, dict) else {}
        by_day[now[:10]] = _as_int(by_day.get(now[:10])) + 1
        for stale in sorted(by_day)[:-USAGE_DAYS_KEPT]:
            by_day.pop(stale, None)
        data["by_day"] = by_day
        data["script_version"] = SCRIPT_VERSION
        data["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(USAGE_FILE)
    except Exception as exc:
        # A statistics write must never take the real work down with it; it is
        # only ever surfaced when explicitly asked for.
        if os.environ.get("EDUCODER_DEBUG"):
            print(f"usage counter skipped: {exc}", file=sys.stderr)


def scan_sessions(root: Path) -> dict:
    """Count agent-side usage of this skill from local session logs.

    Three signals, all read from `~/.pi/agent/sessions/**/*.jsonl`:
    an injected `<skill name="..." >` block (the skill was loaded), a tool call
    touching educoder.py or SKILL.md, and an explicit `/skill:...` command.
    Read-only: nothing is sent anywhere.

    Mentions inside a system prompt are deliberately ignored -- an installed
    skill is advertised in every session, so counting those would measure
    installation rather than use.
    """
    stats = {
        "sessions_dir": str(root),
        "found": root.is_dir(),
        "files_scanned": 0,
        "sessions_touched": 0,
        "skill_loads": 0,
        "tool_calls": 0,
        "slash_commands": 0,
        "first_seen": "",
        "last_seen": "",
    }
    if not stats["found"]:
        return stats
    for path in sorted(root.rglob("*.jsonl")):
        stats["files_scanned"] += 1
        touched = False
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "message":
                    continue
                message = event.get("message") or {}
                role = message.get("role")
                content = message.get("content")
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not isinstance(content, list):
                    continue
                line_hit = False
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    kind = part.get("type")
                    if role == "user" and kind == "text":
                        text = part.get("text") or ""
                        if f'<skill name="{SKILL_NAME}"' in text:
                            stats["skill_loads"] += 1
                            line_hit = True
                        if re.search(rf"(^|\n)\s*/skill:{SKILL_NAME}\b", text):
                            stats["slash_commands"] += 1
                            line_hit = True
                    elif kind in ("toolCall", "tool_call"):
                        raw_args = part.get("arguments")
                        blob = (raw_args if isinstance(raw_args, str)
                                else json.dumps(raw_args or {}, ensure_ascii=False))
                        if "educoder.py" in blob or f"{SKILL_NAME}/SKILL.md" in blob:
                            stats["tool_calls"] += 1
                            line_hit = True
                if line_hit:
                    touched = True
                    stamp = str(event.get("timestamp") or "")
                    if stamp:
                        if not stats["first_seen"] or stamp < stats["first_seen"]:
                            stats["first_seen"] = stamp
                        if stamp > stats["last_seen"]:
                            stats["last_seen"] = stamp
        if touched:
            stats["sessions_touched"] += 1
    return stats


def cmd_stats(args):
    """Report how often this skill was used, from local data only."""
    local = _read_usage_file()
    report = {"usage_file": str(USAGE_FILE), "local": local}
    if getattr(args, "sessions", False):
        # The directory is caller-supplied on purpose: scanning it is the whole
        # point of the flag. Only *.jsonl files below it are read -- nothing is
        # written, and read paths are absolutised before use.
        raw_root = getattr(args, "sessions_dir", None)
        root = Path(raw_root).expanduser() if raw_root else DEFAULT_SESSIONS_DIR
        report["agent_sessions"] = scan_sessions(root.resolve())
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"local counters: {USAGE_FILE}")
    if not local:
        print("  (none yet - counters are written after the first command)")
    else:
        print(f"  total runs: {local.get('total_runs')}   "
              f"failures: {local.get('failures') or 0}")
        print(f"  first: {local.get('first_used_at')}   "
              f"last: {local.get('last_used_at')}")
        for name, count in sorted((local.get("commands") or {}).items(),
                                  key=lambda kv: (-kv[1], kv[0])):
            print(f"    {name:<10} {count}")
        by_day = local.get("by_day") or {}
        if by_day:
            recent = ", ".join(f"{d}:{by_day[d]}" for d in sorted(by_day)[-7:])
            print(f"  recent days: {recent}")
    agent = report.get("agent_sessions")
    if agent is not None:
        print(f"agent sessions: {agent['sessions_dir']}")
        if not agent["found"]:
            print("  (directory not found - pass --sessions-dir)")
        else:
            print(f"  skill loads: {agent['skill_loads']}   "
                  f"tool calls: {agent['tool_calls']}   "
                  f"/skill: commands: {agent['slash_commands']}")
            print(f"  sessions using it: {agent['sessions_touched']}"
                  f"/{agent['files_scanned']} scanned")
            if agent["first_seen"]:
                print(f"  first: {agent['first_seen']}   "
                      f"last: {agent['last_seen']}")
    print("note: counters are local to this machine; nothing is uploaded.")
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_login(args):
    api = Api()
    password = args.password or os.environ.get("EDUCODER_PASSWORD")
    if not password:
        import getpass
        password = getpass.getpass("password: ")
    data = api.login(args.login, password)
    print(f"login ok: {data.get('name')} (user_id={data.get('user_id')}, "
          f"school={data.get('school')})")
    print(f"session cached -> {SESSION_FILE}")


def cmd_status(args):
    api = Api()
    if not api.load():
        print("no cached session")
        return 1
    try:
        data = api.get(f"/courses.json?page=1&limit=1&order=mine&search=&zzud={api.zzud}")
    except SessionExpired:
        print("cached session EXPIRED -> run login again")
        return 1
    except ApiError as exc:
        print(f"session check failed: {exc}")
        return 1
    print(f"session OK (zzud={api.zzud}), courses visible: "
          f"{len(data.get('courses') or [])}")


def cmd_courses(args):
    api = Api()
    api.ensure()
    items = fetch_courses(api, refresh=args.refresh)

    def render(_):
        for c in items:
            print(f"{c.get('identifier'):>10}  id={c.get('id'):<7} "
                  f"tasks={c.get('tasks_count'):<4} "
                  f"{'ended' if c.get('is_end') else 'active':<7} {c.get('name')}")

    emit(items, args.json, render, args.fields)


def cmd_homeworks(args):
    api = Api()
    api.ensure()
    course = resolve_course(api, args.course, refresh=args.refresh)
    cid = course["identifier"]
    items = fetch_homeworks(api, cid, refresh=args.refresh)

    def render(_):
        print(f"course: {course.get('name')} ({cid})")
        for h in items:
            total = h.get("challenge_count") or 0
            done = h.get("finished_challenge_count") or 0
            flag = "DONE" if total and done >= total else "TODO"
            # Resolving the first challenge id here saves a later round trip.
            gid = entry_game_id(api, h) if flag == "TODO" else "-"
            print(f"  [{flag}] hw={h.get('homework_id'):<8} {done}/{total:<3} "
                  f"gid={gid} {h.get('name')}")

    emit(items, args.json, render, args.fields)


def _task_view(api: Api, game_id: str, hw_id: int | None) -> dict:
    raw = task_detail(api, game_id, hw_id)
    return raw


def cmd_task(args):
    api = Api()
    api.ensure()
    game_id = args.game
    hw_id = args.homework_id
    if not game_id:
        course = resolve_course(api, args.course)
        homework = resolve_homework(api, course, args.homework)
        hw_id = homework["homework_id"]
        game_id = entry_game_id(api, homework)
    if not game_id:
        raise ApiError("无法解析该作业的首个关卡，请用 --game 指定")
    raw = _task_view(api, game_id, hw_id)

    if args.json:
        print(json.dumps(select_fields(raw, args.fields), ensure_ascii=False, indent=2))
        return

    game = raw.get("game") or {}
    challenge = raw.get("challenge") or {}
    myshixun = raw.get("myshixun") or {}
    title = (challenge.get("subject") or game.get("name")
             or raw.get("homework_common_name"))
    print(f"# {title}")
    print(f"game_id={game.get('id')} challenge_id={challenge.get('id')} "
          f"challenge_path={challenge.get('path')}")
    print(f"myshixun={myshixun.get('identifier')} hw_id={raw.get('homework_common_id')} "
          f"sec_key={'yes' if raw.get('sec_key') else 'no'} "
          f"with_code_file={challenge.get('with_code_file')}")
    print(f"prev_game={raw.get('prev_game')} next_game={raw.get('next_game')} "
          f"game_count={raw.get('game_count')}")
    files = split_paths(challenge.get("path"))
    if files:
        print("files: " + ", ".join(files))

    ct_tests = (raw.get("choose_test_cases") or {}).get("test_sets") or []
    if ct_tests:
        print(f"\n--- choice questions ({len(ct_tests)}) ---")
        for t in ct_tests:
            print(f"  pos={t.get('position')} answer={t.get('standard_answer')!r} "
                  f"type={t.get('question_name')} submitted={t.get('result')}")
        answers = [t.get("standard_answer") for t in ct_tests]
        print(f"  answers={json.dumps(answers, ensure_ascii=False)}")

    tests = raw.get("test_sets") or []
    if tests:
        print(f"\n--- test sets ({len(tests)}) ---")
        for t in tests[:5]:
            print(f"[{t.get('position')}] in={str(t.get('input'))[:200]!r} "
                  f"expect={str(t.get('output'))[:200]!r}")

    if raw.get("last_compile_output"):
        print("\n--- last compile output ---")
        print(str(raw["last_compile_output"])[:1500])

    # A choice-only challenge is fully specified by its answers; the statement is
    # long and adds nothing. Opt in with --statement.
    if ct_tests and not args.statement:
        print("\n(statement omitted for choice challenge; use --statement to print it)")
        return
    # The statement lives in challenge.task_pass despite the misleading name.
    desc = strip_html(challenge.get("description") or challenge.get("task_pass")
                      or game.get("description") or "")
    if desc:
        print("\n--- statement ---")
        print(desc if args.statement else bounded(desc, args.max_output))

    if args.with_code:
        files = split_paths(challenge.get("path"))
        if not files:
            print("\n(no code files on this challenge)")
        for remote in files:
            text = read_remote_file(api, game_id, hw_id, remote)
            print(f"\n########## {remote} ##########")
            print(bounded(text, args.max_output))


def read_remote_file(api: Api, game_id: str, hw_id, remote: str) -> str:
    """Read one remote answer file (the platform returns base64)."""
    data = api.get(
        f"/tasks/{game_id}/rep_content.json"
        f"?path={urllib.parse.quote(str(remote))}"
        f"&homework_common_id={hw_id or ''}&exercise_id=&zzud={api.zzud}"
    )
    content = ((data.get("content") or {}) or {}).get("content") or ""
    return base64.b64decode(content).decode("utf-8", "replace") if content else ""


def cmd_code(args):
    api = Api()
    api.ensure()
    game_id = args.game
    hw_id = args.homework_id
    if not game_id:
        course = resolve_course(api, args.course, refresh=args.refresh)
        homework = resolve_homework(api, course, args.homework, refresh=args.refresh)
        hw_id = homework["homework_id"]
        game_id = entry_game_id(api, homework)
    if not game_id:
        raise ApiError("无法解析关卡，请用 --game 指定")
    challenge = (_task_view(api, game_id, hw_id)).get("challenge") or {}
    targets = [args.path] if args.path else split_paths(challenge.get("path"))
    if not targets:
        raise ApiError("该关卡没有代码文件，请用 --path 指定")

    as_dir = bool(args.output) and args.output.endswith(("/", "\\"))
    for remote in targets:
        text = read_remote_file(api, game_id, hw_id, remote)
        if not args.output:
            print(f"########## {remote} ##########")
            print(bounded(text, args.max_output))
            continue
        dest = Path(args.output) / Path(remote).name if as_dir else Path(args.output)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"wrote {len(text)} -> {dest}  (remote: {remote})")


def load_answers(path: str) -> list[str]:
    """Read answers from a JSON list, a {position: answer} map, or one per line.

    A file beats 100+ positional arguments: it survives shell quoting, can be
    reviewed before submission, and can be replayed after a page-side redo.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Windows editors still save GBK by default; an answer file is data, so
        # fall back instead of crashing on it.
        try:
            text = Path(path).read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError) as exc:
            raise ApiError(f"--answer-file 无法解码（需 UTF-8 或 GBK）：{exc}") from None
    except OSError as exc:
        raise ApiError(f"无法读取 --answer-file：{exc}") from None
    stripped = text.strip()
    if not stripped:
        raise ApiError(f"--answer-file 为空：{path}")
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            raise ApiError(f"--answer-file 不是合法 JSON：{exc}") from None
        if isinstance(data, list):
            return [str(item).strip() for item in data]
        if isinstance(data, dict):
            try:
                ordered = sorted(data.items(), key=lambda kv: int(kv[0]))
            except (TypeError, ValueError):
                raise ApiError("--answer-file 的键必须是题号（1、2、3…）") from None
            return [str(value).strip() for _, value in ordered]
        raise ApiError("--answer-file JSON 必须是数组或 {题号: 答案} 对象")
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def summarize_wrong(tests: list[dict]) -> list[tuple]:
    """Return (position, mine, standard) for every non-passing answer.

    `result` is None while a question is ungraded, so only an explicitly falsy
    result counts as wrong.
    """
    wrong = []
    for t in tests:
        result = t.get("result")
        if result is None or result:
            continue
        wrong.append((t.get("position"), t.get("actual_output"),
                      t.get("standard_answer")))
    return wrong


def cmd_choose(args):
    api = Api()
    api.ensure()
    gid = args.game
    if not gid:
        course = resolve_course(api, args.course)
        homework = resolve_homework(api, course, args.homework)
        gid = entry_game_id(api, homework)
    if not gid:
        raise ApiError("请用 --game 指定关卡")
    raw = _task_view(api, gid, args.homework_id)
    ch = raw.get("challenge") or {}
    tests = (raw.get("choose_test_cases") or {}).get("test_sets") or []

    if getattr(args, "answer_file", None):
        answers = load_answers(args.answer_file)
    elif args.answer:
        answers = list(args.answer)
    else:
        answers = [t.get("standard_answer") for t in tests]
    if not answers or any(a is None for a in answers):
        raise ApiError("该关卡没有可提交的选择题答案（平台未下发 standard_answer）；"
                       "用 --answer-file 提供答案，客观题流程见 references/quiz.md")

    expected = len(tests)
    if expected and len(answers) != expected:
        raise ApiError(f"答案数量 {len(answers)} 与题目数量 {expected} 不一致，拒绝提交")

    if getattr(args, "dry_run", False):
        preview = ", ".join(f"{i + 1}:{a}" for i, a in enumerate(answers[:8]))
        more = f" … (+{len(answers) - 8})" if len(answers) > 8 else ""
        print(f"DRY-RUN game={gid} challenge={ch.get('id')} "
              f"questions={expected} answers={len(answers)}")
        print(f"  {preview}{more}")
        return 0

    resp = api.post(
        f"/tasks/{gid}/choose_build.json?zzud={api.zzud}",
        {"answer": answers, "challenge_id": ch.get("id"),
         "subject_id": args.subject_id or "", "question_id": None,
         "competition_entry_id": None, "smart_plan_page_item_bank_id": None},
    )
    if resp.get("status") not in (None, 0) and resp.get("message"):
        raise ApiError(f"提交被拒绝：{resp.get('message')}"
                       "（这类校验只有页面提交能通过，见 references/quiz.md）")

    total = resp.get("challenge_chooses_count")
    correct = resp.get("choose_correct_num")
    print(f"answers={json.dumps(answers, ensure_ascii=False)} correct={correct}/{total} "
          f"gold={resp.get('gold')} next_game={resp.get('next_game')}")
    for t in resp.get("test_sets") or []:
        print(f"  pos={t.get('position')} result={t.get('result')} "
              f"actual={t.get('actual_output')!r} std={t.get('standard_answer')!r}")
    wrong = summarize_wrong(resp.get("test_sets") or [])
    if wrong:
        detail = "; ".join(f"pos={p} mine={m!r} std={s!r}" for p, m, s in wrong)
        print(f"wrong={len(wrong)}/{total}: {detail}")
    return 0 if total and correct == total else 1


def _snippet(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def divergence(actual: str, expect: str) -> str:
    """Describe where actual output first differs from the expected output.

    Truncating both sides to N chars hides the interesting part whenever the
    divergence sits past N, which forces an extra round trip to inspect the
    full payload. Locating the first difference instead keeps the failure
    report both short and sufficient to act on.
    """
    a_lines = actual.splitlines()
    e_lines = expect.splitlines()
    for i in range(max(len(a_lines), len(e_lines))):
        av = a_lines[i] if i < len(a_lines) else None
        ev = e_lines[i] if i < len(e_lines) else None
        if av == ev:
            continue
        if av is None:
            rest = e_lines[i:i + 3]
            tail = " | ".join(_snippet(line, 60) for line in rest)
            hidden = len(e_lines) - i - len(rest)
            more = f" (+{hidden} more lines)" if hidden > 0 else ""
            return f"ends early at line {i + 1}; expected {tail}{more}"
        if ev is None:
            extra = a_lines[i:i + 3]
            tail = " | ".join(_snippet(line, 60) for line in extra)
            hidden = len(a_lines) - i - len(extra)
            more = f" (+{hidden} more lines)" if hidden > 0 else ""
            return f"extra output at line {i + 1}: {tail}{more}"
        spot = next((k for k in range(min(len(av), len(ev))) if av[k] != ev[k]),
                    min(len(av), len(ev)))
        lo = max(0, spot - 20)
        return (f"line {i + 1} col {spot + 1}: "
                f"expected {_snippet(ev[lo:spot + 60], 90)!r} "
                f"got {_snippet(av[lo:spot + 60], 90)!r}")
    return "text matches; check exit status or time limit"


# --------------------------------------------------------------------------
# shared write path: one save per file, one grading run per challenge
# --------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r"[\u2014\u2015]{2,}")


def placeholder_hit(text: str) -> str | None:
    """Describe the first platform blank left in `text`, if any.

    The platform writes its blanks as runs of U+2014/U+2015, which is not legal
    Java: uploading one again guarantees a compile failure and burns an
    evaluation quota, so refuse it locally (0 requests) instead.
    """
    for i, line in enumerate(text.splitlines(), 1):
        if PLACEHOLDER_RE.search(line):
            return f"line {i}: {line.strip()[:60]}"
    return None


def build_extras(challenge: dict, hw_id, user: dict) -> dict:
    return {
        "challenge_id": challenge["id"],
        "homework_common_id": hw_id,
        "currentUserId": user["user_id"],
        "exercise_id": "", "question_id": "", "subject_id": "",
        "competition_entry_id": "",
    }


def resolve_target(api: Api, args):
    """Return (game_id, homework_common_id) from explicit ids or selectors."""
    game_id = getattr(args, "game", None)
    hw_id = getattr(args, "homework_id", None)
    if game_id:
        return game_id, hw_id
    refresh = bool(getattr(args, "refresh", False))
    course = resolve_course(api, args.course, refresh=refresh)
    homework = resolve_homework(api, course, args.homework, refresh=refresh)
    return entry_game_id(api, homework), homework["homework_id"]


def save_file(api: Api, mid: str, hw_id, game_num_id, extras: dict,
              remote: str, content: str, *, evaluate: int = 0) -> str:
    """Write one remote file; returns its commit id.

    evaluate=0 by default on purpose: the CLI triggers grading itself, once per
    challenge. Letting the save call grade too yields one run per uploaded
    file, i.e. a run against a half-applied workspace for every file but the
    last.
    """
    resp = api.post(
        f"/myshixuns/{mid}/update_file.json?zzud={api.zzud}",
        {"path": remote, "evaluate": evaluate, "content": content,
         "game_id": game_num_id, "tab_type": 1,
         "homework_common_id": hw_id, "extras": extras},
    )
    return str(((resp.get("content") or {}) or {}).get("commitID") or "")


def trigger_evaluate(api: Api, game_id: str, raw: dict, extras: dict,
                     commit: str, env_id) -> dict:
    """Start one grading run and return the platform's `res` object."""
    resp = api.post(
        f"/tasks/{game_id}/game_build.json?zzud={api.zzud}",
        {"sec_key": raw.get("sec_key") or "", "resubmit": "", "first": 1,
         "content_modified": 0, "shixun_environment_id": env_id, "tab_type": 1,
         "extras": {**extras, "commitID": commit}},
    )
    return resp.get("res") or {}


SLIM_VIEW_KEYS = ("game", "test_sets", "last_compile_output")


def _slim_view(raw: dict) -> dict:
    """Keep only what a verdict needs: the statement and file payloads never
    change while polling, and report_result reads nothing else."""
    return {k: raw.get(k) for k in SLIM_VIEW_KEYS}


def _verdict_key(cur: dict):
    """Settled-verdict fingerprint, or None while grading still runs.

    None means at least one test case has no result yet. Otherwise the tuple
    covers everything a verdict consists of, so two identical fingerprints mean
    the platform has stopped changing its mind.
    """
    tests = cur.get("test_sets") or []
    rated = [t for t in tests if t.get("result") is not None]
    if not tests or len(rated) != len(tests):
        return None
    game = cur.get("game") or {}
    return (game.get("status"), game.get("final_score"), game.get("accuracy"),
            tuple(bool(t.get("result")) for t in tests))


def wait_for_verdict(api: Api, game_id: str, hw_id, timeout: int) -> bool:
    """Poll until the platform reports a *settled* verdict.

    A run can publish a fresh `test_sets` while `game` still carries the
    pre-run status and score: a passing run was observed reporting
    "OK status=0 score=0 accuracy=None" mid-flight. So a verdict is accepted
    only when `status == 2`, or when two consecutive polls agree on both the
    results and the game fields.
    """
    deadline = time.time() + timeout
    prev_key = None
    while time.time() < deadline:
        time.sleep(2)
        cur = _slim_view(_task_view(api, game_id, hw_id))
        if (cur.get("game") or {}).get("status") == 2:
            report_result(cur)
            return True
        key = _verdict_key(cur)
        if key and key == prev_key:
            report_result(cur)
            return True
        prev_key = key
    print("wait timeout; re-run `task --game` to inspect")
    return False


def match_dir_to_remote(local_dir: Path, remote_paths: list[str]):
    """Pair files under local_dir with a challenge's remote paths.

    A path-suffix match wins (src/step2/MeshAir.java), so same-named files in
    different steps never collide; a unique basename match is the fallback for
    the flat work directory of the documented flow. Returns
    (pairs, unmatched_locals, ambiguous) so the caller can refuse instead of
    guessing.
    """
    locals_ = sorted(p for p in local_dir.rglob("*") if p.is_file())
    pairs, taken, ambiguous = [], set(), []
    for remote in remote_paths:
        rel = str(remote).replace("\\", "/").lstrip("/")
        exact = [p for p in locals_ if p.as_posix().endswith("/" + rel)]
        cand = exact or [p for p in locals_ if p.name == Path(rel).name]
        if not cand:
            continue
        if len(cand) > 1:
            ambiguous.append((remote, [str(p) for p in cand[:3]]))
            continue
        pairs.append((remote, cand[0]))
        taken.add(cand[0])
    return pairs, [p for p in locals_ if p not in taken], ambiguous


def report_result(cur: dict) -> bool:
    """Print the evaluation outcome; True once the platform has a verdict.

    `test_sets` alone is NOT a verdict: the platform already returns the list
    with result=null while evaluation is still running, so treating its mere
    presence as terminal would make --wait return before the run finished.
    """
    game = cur.get("game") or {}
    tests = cur.get("test_sets") or []
    rated = [t for t in tests if t.get("result") is not None]
    complete = bool(tests) and len(rated) == len(tests)
    if game.get("status") != 2 and not complete:
        return False
    passed = all(t.get("result") for t in rated) if rated else True
    print(f"{'OK' if passed else 'FAIL'} status={game.get('status')} "
          f"score={game.get('final_score')} accuracy={game.get('accuracy')}")
    for t in tests:
        if not t.get("result"):
            print(f"  pos={t.get('position')} "
                  f"{divergence(str(t.get('actual_output') or ''), str(t.get('output') or ''))}")
    if not passed:
        out = str(cur.get("last_compile_output") or "")
        if out:
            print(f"  compile: {bounded(out, 400)}")
    return True


def cmd_submit(args):
    """Save code and grade it once.

    Multi-file challenges are graded as a whole, so --dir saves every matching
    file with evaluate=0 and triggers a single run at the end: one run per
    uploaded file would grade a half-applied workspace and waste the quota.
    """
    api = Api()
    api.ensure()
    game_id, hw_id = resolve_target(api, args)
    if not game_id:
        raise ApiError("无法解析关卡，请用 --game 指定")
    raw = _task_view(api, game_id, hw_id)

    game = raw.get("game") or {}
    challenge = raw.get("challenge") or {}
    myshixun = raw.get("myshixun") or {}
    user = raw.get("user") or {}
    envs = raw.get("shixun_environments") or []
    remote_paths = split_paths(challenge.get("path"))

    # Validate everything BEFORE the first write. A half-applied submit (file
    # saved remotely, evaluation never triggered) is worse than a refusal.
    missing = []
    if not myshixun.get("identifier"):
        missing.append("myshixun identifier")
    if not challenge.get("id"):
        missing.append("challenge id")
    if not user.get("user_id"):
        missing.append("current user id")
    if not envs:
        missing.append("shixun environment")

    if args.dir:
        local_dir = Path(args.dir)
        if not local_dir.is_dir():
            raise ApiError(f"--dir 不是目录：{args.dir}")
        if not remote_paths:
            raise ApiError("该关卡没有远端代码文件，无法用 --dir 提交")
        pairs, skipped, ambiguous = match_dir_to_remote(local_dir, remote_paths)
        if ambiguous:
            detail = "; ".join(f"{r} <= {', '.join(c)}" for r, c in ambiguous[:3])
            raise ApiError(f"本地文件与远端路径匹配不唯一，请收紧目录结构：{detail}")
        if not pairs:
            raise ApiError(f"{args.dir} 下没有文件匹配远端路径："
                           + ", ".join(remote_paths[:6]))
    else:
        path = args.path or (remote_paths[0] if remote_paths else None)
        if not args.file:
            missing.append("本地文件(--file)")
        if not path:
            missing.append("远端路径(--path)")
        pairs = [(path, Path(args.file))] if (path and args.file) else []
        skipped, ambiguous = [], []
    if missing:
        raise ApiError("拒绝提交，缺少：" + "、".join(missing))

    plans = []
    for remote, local in pairs:
        content = local.read_text(encoding="utf-8")
        if not content.strip():
            raise ApiError(f"本地文件为空：{local}")
        if not args.force:
            hit = placeholder_hit(content)
            if hit:
                raise ApiError(
                    f"拒绝提交 {local.name}（{remote}）：内容仍是平台占位符 "
                    f"[{hit}]，上传后必然编译失败。补全后再提交，确需原样上传时加 --force")
        plans.append((remote, local, content))

    env_id = envs[0].get("shixun_environment_id")
    if args.dry_run:
        print(f"DRY-RUN game={game_id} challenge={challenge['id']} env={env_id} "
              f"evaluate={not args.no_evaluate} files={len(plans)}")
        for remote, local, content in plans:
            print(f"  {remote} <- {local} "
                  f"({len(content.encode('utf-8'))} bytes)")
        for local in skipped:
            print(f"  (no matching remote path) {local}")
        return

    extras = build_extras(challenge, hw_id, user)
    mid = myshixun["identifier"]
    commit = ""
    for remote, _local, content in plans:
        commit = save_file(api, mid, hw_id, game.get("id"), extras, remote, content)
        print(f"save: path={remote} commit={commit[:12]}")
    if skipped:
        names = ", ".join(p.name for p in skipped[:5])
        print(f"note: {len(skipped)} 个本地文件未匹配远端路径：{names}")

    if args.no_evaluate:
        return
    res = trigger_evaluate(api, game_id, raw, extras,
                           commit or str(myshixun.get("commit_id") or ""), env_id)
    print(f"evaluate: code={res.get('code')} "
          f"msg={((res.get('data') or {}) or {}).get('msg')}")
    if args.wait:
        wait_for_verdict(api, game_id, hw_id, args.timeout)


def cmd_evaluate(args):
    """Re-run grading for the current commit, without re-uploading anything.

    Retrying a run by re-saving the file resends the whole content and creates
    a new commit; grading only needs the commit id, which
    /tasks/{gid}.json exposes as myshixun.commit_id. Use this when a verdict
    looks stale, e.g. the compile log still quotes the pre-edit content.
    """
    api = Api()
    api.ensure()
    game_id, hw_id = resolve_target(api, args)
    if not game_id:
        raise ApiError("无法解析关卡，请用 --game 指定")
    raw = _task_view(api, game_id, hw_id)

    challenge = raw.get("challenge") or {}
    myshixun = raw.get("myshixun") or {}
    user = raw.get("user") or {}
    envs = raw.get("shixun_environments") or []
    commit = args.commit or str(myshixun.get("commit_id") or "")

    missing = []
    if not challenge.get("id"):
        missing.append("challenge id")
    if not user.get("user_id"):
        missing.append("current user id")
    if not envs:
        missing.append("shixun environment")
    if not commit:
        missing.append("commit id（--commit）")
    if missing:
        raise ApiError("拒绝评测，缺少：" + "、".join(missing))

    env_id = envs[0].get("shixun_environment_id")
    if args.dry_run:
        print(f"DRY-RUN evaluate game={game_id} challenge={challenge['id']} "
              f"commit={commit[:12]} env={env_id}")
        return

    extras = build_extras(challenge, hw_id, user)
    res = trigger_evaluate(api, game_id, raw, extras, commit, env_id)
    print(f"evaluate: code={res.get('code')} "
          f"msg={((res.get('data') or {}) or {}).get('msg')}")
    if args.wait:
        wait_for_verdict(api, game_id, hw_id, args.timeout)



def walk_challenges(api: Api, entry_gid: str, hw_id):
    """Yield (gid, detail) for every challenge of a homework, in platform order.

    shixun_exec.json hands back the last *visited* challenge, not the first
    unfinished one -- observed on a 3/4 homework whose entry sat on the finished
    pos=4, which made a naive walk report "all done" while an earlier challenge
    was still open. Rewinding prev_game first costs a few reads but is the only
    way to see challenges the entry point skips. Details fetched during the
    rewind are reused on the way forward, so each challenge is read once.
    """
    cache: dict[str, dict] = {}

    def detail_of(gid: str) -> dict:
        if gid not in cache:
            cache[gid] = task_detail(api, gid, hw_id)
        return cache[gid]

    chain: list[str] = []
    gid = entry_gid
    while gid and gid not in chain:
        chain.append(gid)
        gid = str(detail_of(gid).get("prev_game") or "")

    emitted: set[str] = set()
    queue = list(reversed(chain))
    while queue:
        gid = queue.pop(0)
        if gid in emitted:
            continue
        emitted.add(gid)
        detail = detail_of(gid)
        yield gid, detail
        nxt = detail.get("next_game")
        if nxt:
            queue.insert(0, str(nxt))


def cmd_auto(args):
    """Walk every challenge of a homework and report which ones still need work."""
    api = Api()
    api.ensure()
    course = resolve_course(api, args.course, refresh=args.refresh)
    homework = resolve_homework(api, course, args.homework, refresh=args.refresh)
    hw_id = homework["homework_id"]
    entry = entry_game_id(api, homework)
    if not entry:
        raise ApiError("无法解析该作业的入口关卡")

    walked = 0
    todo = 0
    for _gid, raw in walk_challenges(api, entry, hw_id):
        game = raw.get("game") or {}
        walked += 1
        ch = raw.get("challenge") or {}
        passed = game.get("status") == 2
        if not passed:
            todo += 1
            if args.next:
                # --next answers the only question a finishing loop asks
                # ("where do I go now?") in one line: no statement, no file
                # list, so it stays cheap across many rounds.
                print(game.get("identifier"))
                return
        if args.next:
            continue
        kind = "choice" if (raw.get("choose_test_cases") or {}).get("test_sets") else "code"
        print(f"[{'PASS' if passed else 'TODO'}] {ch.get('subject') or game.get('id')} "
              f"kind={kind} gid={game.get('identifier')} "
              f"score={game.get('final_score')} "
              f"files={split_paths(ch.get('path')) or '-'}")
    if args.next:
        print("-")
        return
    print(f"\n{walked} challenges walked; {todo} still open")


def cmd_solve(args):
    """Walk a homework: auto-submit choice challenges, list the code left over."""
    api = Api()
    api.ensure()
    course = resolve_course(api, args.course, refresh=args.refresh)
    homework = resolve_homework(api, course, args.homework, refresh=args.refresh)
    hw_id = homework["homework_id"]
    entry = entry_game_id(api, homework)
    if not entry:
        raise ApiError("无法解析该作业的入口关卡")

    passed = 0
    total = 0
    code_todo: list[str] = []
    for gid, raw in walk_challenges(api, entry, hw_id):
        game = raw.get("game") or {}
        ch = raw.get("challenge") or {}
        title = str(ch.get("subject") or game.get("id") or "(untitled)")
        total += 1
        if game.get("status") == 2:
            passed += 1
            print(f"[PASS] {title}")
            continue
        tests = (raw.get("choose_test_cases") or {}).get("test_sets") or []
        answers = [t.get("standard_answer") for t in tests]
        if tests and not args.no_choice and all(a is not None for a in answers):
            resp = api.post(
                f"/tasks/{gid}/choose_build.json?zzud={api.zzud}",
                {"answer": answers, "challenge_id": ch.get("id"),
                 "subject_id": args.subject_id, "question_id": None,
                 "competition_entry_id": None,
                 "smart_plan_page_item_bank_id": None},
            )
            ok = resp.get("choose_correct_num") == resp.get("challenge_chooses_count")
            if ok:
                passed += 1
            print(f"[{'PASS' if ok else 'FAIL'}] {title} choice "
                  f"{resp.get('choose_correct_num')}/{resp.get('challenge_chooses_count')}")
        else:
            files = split_paths(ch.get("path"))
            code_todo.append(title)
            print(f"[TODO] {title} gid={game.get('identifier')} files={len(files)} "
                  f"{', '.join(files) if files else '-'}")
    print(f"\npassed {passed}/{total}; code work left: {len(code_todo)}")


# --------------------------------------------------------------------------
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="raw JSON output")
    common.add_argument("--fields",
                        help="comma-separated top-level keys to keep in --json output")
    common.add_argument("--refresh", action="store_true",
                        help="bypass the course/homework cache")
    common.add_argument("--max-output", type=int, default=MAX_OUTPUT_DEFAULT,
                        help=f"truncate printed text (default {MAX_OUTPUT_DEFAULT} chars)")

    p = argparse.ArgumentParser(prog="educoder.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="login and cache session")
    s.add_argument("--login", required=True)
    s.add_argument("--password")
    s.set_defaults(fn=cmd_login)

    sub.add_parser("status", help="check cached session").set_defaults(fn=cmd_status)
    sub.add_parser("courses", parents=[common],
                   help="list courses").set_defaults(fn=cmd_courses)

    s = sub.add_parser("homeworks", parents=[common], help="list homeworks in a course")
    s.add_argument("--course", required=True)
    s.set_defaults(fn=cmd_homeworks)

    s = sub.add_parser("task", parents=[common], help="show challenge detail")
    s.add_argument("--course")
    s.add_argument("--homework")
    s.add_argument("--game")
    s.add_argument("--homework-id", type=int)
    s.add_argument("--statement", action="store_true",
                   help="print the full statement (skipped for choice challenges)")
    s.add_argument("--with-code", action="store_true",
                   help="also print every code file of the challenge")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("choose", parents=[common],
                       help="submit choice answers (platform's own, or --answer-file)")
    s.add_argument("--game")
    s.add_argument("--course")
    s.add_argument("--homework")
    s.add_argument("--homework-id", type=int)
    s.add_argument("--subject-id")
    s.add_argument("--answer", nargs="*")
    s.add_argument("--answer-file",
                   help="answers from a JSON list, a {position: answer} map, "
                        "or one per line (# comments allowed)")
    s.add_argument("--dry-run", action="store_true",
                   help="validate answer count/format without submitting")
    s.set_defaults(fn=cmd_choose)

    s = sub.add_parser("code", parents=[common], help="read remote answer file(s)")
    s.add_argument("--course")
    s.add_argument("--homework")
    s.add_argument("--game")
    s.add_argument("--homework-id", type=int)
    s.add_argument("--path", help="one remote file (default: all files of the challenge)")
    s.add_argument("--output", help="file or dir to write into (default: stdout)")
    s.set_defaults(fn=cmd_code)

    s = sub.add_parser("submit", parents=[common],
                       help="save code and grade it (one grading run per challenge)")
    s.add_argument("--course")
    s.add_argument("--homework")
    s.add_argument("--game")
    s.add_argument("--homework-id", type=int)
    s.add_argument("--path", help="remote file to overwrite (default: first file)")
    s.add_argument("--file", help="local file whose content is uploaded")
    s.add_argument("--dir", help="local directory: upload every file matching a "
                                 "remote path of this challenge, then grade once")
    s.add_argument("--no-evaluate", action="store_true",
                   help="save only; skip the grading run")
    s.add_argument("--force", action="store_true",
                   help="upload even if the content still holds a platform blank")
    s.add_argument("--dry-run", action="store_true",
                   help="validate and print the plan without writing anything")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=int, default=90)
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("evaluate", parents=[common],
                       help="re-run grading for the current commit (no upload)")
    s.add_argument("--course")
    s.add_argument("--homework")
    s.add_argument("--game")
    s.add_argument("--homework-id", type=int)
    s.add_argument("--commit",
                   help="commit id to grade (default: myshixun.commit_id)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=int, default=90)
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("auto", parents=[common], help="walk all challenges of a homework")
    s.add_argument("--course", required=True)
    s.add_argument("--homework", required=True)
    s.add_argument("--next", action="store_true",
                   help="print only the next unfinished game id (or '-' if none)")
    s.set_defaults(fn=cmd_auto)

    s = sub.add_parser("solve", parents=[common],
                       help="walk a homework, auto-submitting choice challenges")
    s.add_argument("--course", required=True)
    s.add_argument("--homework", required=True)
    s.add_argument("--no-choice", action="store_true",
                   help="do not auto-submit choice challenges")
    s.add_argument("--subject-id")
    s.set_defaults(fn=cmd_solve)

    s = sub.add_parser("stats", parents=[common],
                       help="show how often this skill was used (local counters)")
    s.add_argument("--sessions", action="store_true",
                   help="also scan local agent session logs for skill usage")
    s.add_argument("--sessions-dir",
                   help=f"session log root (default {DEFAULT_SESSIONS_DIR})")
    s.set_defaults(fn=cmd_stats)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "json"):
        args.json = False
    if not hasattr(args, "fields"):
        args.fields = None
    if isinstance(args.fields, str):
        args.fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    if not hasattr(args, "refresh"):
        args.refresh = False
    if not hasattr(args, "max_output"):
        args.max_output = MAX_OUTPUT_DEFAULT
    status = 0
    try:
        status = args.fn(args) or 0
    except KeyboardInterrupt:
        status = 130
    except SessionExpired as exc:
        print(f"error: {exc}\nsession expired — run: "
              f"python educoder.py login --login <account>", file=sys.stderr)
        status = 1
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        status = 1
    record_usage(getattr(args, "cmd", None) or "unknown", ok=(status == 0))
    return status


if __name__ == "__main__":
    sys.exit(main())

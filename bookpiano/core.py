from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import socket
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8), "Asia/Shanghai")
ORIGIN = "https://booking.sustech.edu.cn"
QUERY_PATH = "/api/SystemApi/GetMeetingByMeetingRoomList"
ROOMS_PATH = "/api/SystemApi/GetMeetingRoomAll"
STATES = {0: "管理员审批中", 1: "管理员审批通过", 2: "管理员审批拒绝", 3: "暂存",
          4: "取消预订", 5: "预约异常", 6: "活动中", 7: "已结束", 8: "辅导员审批中",
          9: "预约成功，活动待开始", 10: "辅导员审批拒绝"}


class LocalError(Exception):
    pass


class FetchError(Exception):
    def __init__(self, message, retryable=False, kind="data"):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


@dataclass
class Result:
    status: str
    message: str
    exit_code: int = 0


def now_cst():
    return datetime.now(CST)


def default_config():
    return {"version": 1, "room_name": "致诚大会议室", "room_id": "", "verified": False,
            "daily_time": "05:00", "timezone": "Asia/Shanghai", "retry_minutes": 10,
            "max_attempts_per_day": 2, "min_interval_minutes": 10, "timeout_seconds": 20,
            "source_time_mode": "local_z",
            "fields": {"id": "MeetingID", "room": "MeetingRoomID", "title": "Topical",
                       "start": "StartDateTime", "end": "EndDateTime", "state": "State"}}


def write_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)  # Only the exact temporary file created above.


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, OSError) as exc:
        raise LocalError(f"无法读取 {path.name}；已停止，避免错误更新或重复请求。") from exc


def load_config(root):
    config = load_json(root / "config.json")
    if config is None:
        config = default_config()
        write_json(root / "config.json", config)
    try:
        if config["timezone"] != "Asia/Shanghai" or config["daily_time"] != "05:00":
            raise ValueError()
        if config["source_time_mode"] not in {"local_z", "utc"}:
            raise ValueError()
        if not 1 <= config["max_attempts_per_day"] <= 2:
            raise ValueError()
        if config["min_interval_minutes"] < 10 or config["retry_minutes"] < 10:
            raise ValueError()
        if not 1 <= config["timeout_seconds"] <= 30:
            raise ValueError()
        if not isinstance(config["verified"], bool):
            raise ValueError()
        if not isinstance(config["room_id"], str) or not isinstance(config["room_name"], str):
            raise ValueError()
        if set(config["fields"]) != {"id", "room", "title", "start", "end", "state"}:
            raise ValueError()
        if any(not isinstance(v, str) or not v for v in config["fields"].values()):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalError("配置文件格式或请求限制不正确，请恢复配置后再运行。") from exc
    return config


class RunLock:
    """OS byte-range lock releases automatically after a crash; the file may remain."""
    def __init__(self, root):
        self.path = root / ".run.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise BlockingIOError("已有任务运行")
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization token to a redirect target.


def api_post(path, message_type, data, token, timeout=20):
    from .source import READ_PATHS
    if path not in READ_PATHS | {ROOMS_PATH}:
        raise LocalError("仅允许已经识别的只读会议室查询。")
    if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
        raise FetchError("缺少有效登录凭据，请运行首次配置。", kind="auth")
    body = json.dumps({"MessageType": message_type, "MessageID": str(uuid.uuid4()), "Data": data}).encode()
    request = urllib.request.Request(ORIGIN + path, data=body, method="POST", headers={
        "Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
        "Authorization": token, "Language": "zh", "Origin": ORIGIN,
        "Referer": ORIGIN + "/personalservice/index", "User-Agent": "Bookpiano/1.0 (daily read-only export)"})
    try:
        # urllib does not retry these requests automatically.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as res:
            raw = res.read(2_000_001)
            if len(raw) > 2_000_000:
                raise FetchError("响应异常过大，已停止更新。")
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                raise FetchError("网站未返回预约数据；可能需要重新登录。", kind="auth") from None
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308, 401, 403}:
            raise FetchError("登录已失效或无权限，请运行首次配置。", kind="auth") from None
        if exc.code == 429:
            raise FetchError("网站要求降低访问频率，今天停止自动请求。", kind="rate_limit") from None
        if exc.code == 404:
            raise FetchError("已保存的预约接口返回 404，请重新观察网页查询；不会轮流尝试其他接口。", kind="endpoint") from None
        raise FetchError(f"网站返回 HTTP {exc.code}。", retryable=exc.code >= 500, kind="network") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
        raise FetchError("网络连接失败或超时。", retryable=True, kind="network") from None
    validate_envelope(payload)
    return payload


def validate_envelope(payload):
    if not isinstance(payload, dict):
        raise FetchError("响应格式不正确，已保留旧数据。")
    if payload.get("IsSuccess") not in (True, 1):
        code = str(payload.get("ErrorCode", ""))
        if code in {"5001", "5002", "5003", "6001"}:
            raise FetchError("登录已失效或无权限，请运行首次配置。", kind="auth")
        raise FetchError("网站报告查询失败，已保留旧数据。")
    data = payload.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise FetchError("预约响应缺少完整列表，已保留旧数据。")
    if "total" in data:
        try:
            if int(data["total"]) != len(data["rows"]):
                raise ValueError()
        except (TypeError, ValueError):
            raise FetchError("返回列表可能不完整，已停止更新。") from None
    return data["rows"]


def query_bookings(config, day, token):
    from .source import normalize, query_data, validate_profile
    profile = config.get("source_profile")
    validate_profile(profile)
    payload = api_post(profile["path"], profile["message_type"], query_data(profile, day), token, config["timeout_seconds"])
    return normalize(payload, config)


def parse_source_time(value, mode):
    if not isinstance(value, str) or not value:
        raise FetchError("预约时间字段缺失，已保留旧数据。")
    try:
        if mode == "local_z" and value.endswith("Z"):
            result = datetime.fromisoformat(value[:-1]).replace(tzinfo=CST)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=CST)
        return result.astimezone(CST)
    except ValueError:
        raise FetchError("预约时间格式发生变化，已停止更新。") from None


def build_snapshot(payload, config, fetched):
    rows = validate_envelope(payload)
    day = fetched.astimezone(CST).date()
    low = datetime.combine(day, time.min, CST)
    high = low + timedelta(days=1)
    fields = config["fields"]
    bookings = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FetchError("预约列表格式错误。")
        row_room = row.get(fields["room"])
        if row_room is not None and str(row_room) != config["room_id"]:
            raise FetchError("返回了其他会议室的数据，已停止更新。")
        start = parse_source_time(row.get(fields["start"]), config["source_time_mode"])
        end = parse_source_time(row.get(fields["end"]), config["source_time_mode"])
        if end <= start or start >= high or end <= low:
            raise FetchError("预约时间与查询日期不符，已停止更新。")
        title = row.get(fields["title"])
        if title is not None and not isinstance(title, str):
            raise FetchError("活动名称格式异常。")
        state = row.get(fields["state"])
        try:
            state_code = int(state) if state is not None else None
        except (ValueError, TypeError):
            raise FetchError("预约状态格式异常。") from None
        identifier = str(row.get(fields["id"], ""))
        key = identifier or (start.isoformat(), end.isoformat(), title, state_code)
        if key in seen:
            raise FetchError("返回了重复预约，请核实接口格式。")
        seen.add(key)
        bookings.append({"id": identifier, "start": start.isoformat(), "end": end.isoformat(),
                         "title": title or "未提供活动名称", "state_code": state_code,
                         "state": STATES.get(state_code, "未提供状态" if state_code is None else f"未知状态 {state_code}")})
    bookings.sort(key=lambda b: (b["start"], b["end"], b["id"]))
    return {"version": 1, "date": day.isoformat(), "room_id": config["room_id"],
            "room_name": config["room_name"], "source": ORIGIN + "/personalservice/index",
            "fetched_at": fetched.isoformat(), "timezone": "Asia/Shanghai", "bookings": bookings,
            "config_fingerprint": fingerprint(config)}


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def csv_text(snapshot):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["预约日期", "会议室", "开始时间", "结束时间", "活动名称", "预约状态", "数据获取时间"])
    for b in snapshot["bookings"]:
        writer.writerow([safe_cell(v) for v in [snapshot["date"], snapshot["room_name"],
                         b["start"], b["end"], b["title"], b["state"], snapshot["fetched_at"]]])
    return output.getvalue()


def html_text(snapshot=None, error="", demo=False):
    escape = lambda value: html.escape(str(value), quote=True)
    name = snapshot["room_name"] if snapshot else "致诚大会议室"
    day = snapshot["date"] if snapshot else "尚无数据"
    fetched = snapshot["fetched_at"].replace("T", " ") if snapshot else "尚未更新"
    body = ""
    if snapshot:
        for b in snapshot["bookings"]:
            start = datetime.fromisoformat(b["start"])
            end = datetime.fromisoformat(b["end"])
            fmt = "%H:%M" if start.date().isoformat() == day and end.date().isoformat() == day else "%m-%d %H:%M"
            body += f'<tr><td class="slot">{escape(start.strftime(fmt))} — {escape(end.strftime(fmt))}</td><td>{escape(b["title"])}</td><td>{escape(b["state"])}</td></tr>'
    if not body:
        message = "当天暂无预约" if snapshot else "尚未完成首次配置。请打开“预约管理.cmd”并选择配置向导，登录并核实一次当天安排。"
        body = f'<tr><td colspan="3" class="empty">{escape(message)}</td></tr>'
    notice = f'<div class="warning">更新未完成：{escape(error)}。下方保留最近成功数据，请核对日期。</div>' if error else ""
    if demo:
        notice = '<div class="warning">演示数据 · 不代表真实预约，不会启用定时任务。</div>' + notice
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>{escape(name)} · 预约安排</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#182d3c;font:16px/1.65 "Microsoft YaHei",system-ui,sans-serif}}
main{{max-width:1000px;margin:56px auto;padding:0 24px}}.eyebrow{{font-size:12px;letter-spacing:2px;color:#487369}}h1{{font-size:32px;margin:8px 0}}
.meta{{color:#627280;font-size:14px}}.card{{background:white;border:1px solid #dfe6ea;border-radius:16px;margin-top:24px;overflow:hidden}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}th,td{{padding:18px 22px;text-align:left;border-bottom:1px solid #edf0f2;overflow-wrap:anywhere}}
th{{background:#edf4f1;font-size:13px;color:#49665f}}th:first-child{{width:29%}}th:last-child{{width:29%}}.slot{{font-variant-numeric:tabular-nums;font-weight:600}}
.empty{{padding:46px 22px;color:#687986}}.warning{{background:#fff1d8;border:1px solid #efd39a;padding:14px 18px;border-radius:10px;margin-top:18px;color:#805415}}
footer{{font-size:13px;color:#72828d;margin-top:20px}}@media(max-width:600px){{main{{margin:28px auto;padding:0 14px}}h1{{font-size:25px}}th,td{{padding:13px 10px;font-size:13px}}}}
</style></head><body><main><div class="eyebrow">会议室预约 · 本地导出</div><h1>{escape(name)}</h1>
<div class="meta">预约日期：<strong id="data-date">{escape(day)}</strong>　·　北京时间</div><div class="meta">数据获取时间：{escape(fetched)}</div>
<div id="stale" class="warning" hidden>这不是今天的数据。今天的更新尚未完成，请勿按此判断今天是否空闲。</div>{notice}
<div class="card"><table><thead><tr><th>预约时段</th><th>活动名称</th><th>预约状态</th></tr></thead><tbody>{body}</tbody></table></div>
<footer>这是获取时刻的预约快照。之后的新增或取消需运行“手动更新”。取消、拒绝、审批中等状态按原样列出，不代表已成功占用。<br>
来源：booking.sustech.edu.cn　·　此页面不会请求学校网站；更新文件后重新打开或刷新即可。</footer></main>
<script>const d=document.getElementById('data-date').textContent;function checkDate(){{const today=new Intl.DateTimeFormat('en-CA',{{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}}).format(new Date());document.getElementById('stale').hidden=(d===today||d==='尚无数据');}}checkDate();setInterval(checkDate,60000);</script>
</body></html>'''


def fingerprint(config):
    text = json.dumps({k: config.get(k) for k in ("room_id", "room_name", "source_time_mode", "fields", "source_profile")}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def publish(root, snapshot, error=""):
    # Immutable generation directories + an atomic JSON manifest permit crash recovery.
    stamp = datetime.fromisoformat(snapshot["fetched_at"]).strftime("%H%M%S-%f")
    folder = root / "data" / snapshot["date"] / stamp
    write_json(folder / "bookings.json", snapshot)
    write_bytes(folder / "bookings.csv", csv_text(snapshot).encode("utf-8-sig"))
    write_bytes(folder / "report.html", html_text(snapshot).encode("utf-8"))
    for filename in ("bookings.json", "bookings.csv", "report.html"):
        if not (folder / filename).is_file():
            raise LocalError("导出文件不完整。")
    write_json(root / "latest.json", snapshot)
    write_bytes(root / "查看预约.html", html_text(snapshot, error).encode("utf-8"))
    return str(folder.relative_to(root)).replace("\\", "/")


def log(root, when, status, message):
    path = root / "logs" / (when.date().isoformat() + ".log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{when.isoformat()} [{status}] {message}\n")


def _record_success(root, state, snapshot, config):
    folder = publish(root, snapshot)
    state.update(last_success_date=snapshot["date"], last_success_at=snapshot["fetched_at"],
                 last_output=folder, last_fingerprint=fingerprint(config), last_error="", blocked=False,
                 next_retry_at=None, status="success")
    write_json(root / "state.json", state)
    (root / "pending.json").unlink(missing_ok=True)
    return Result("success", f"已导出 {snapshot['date']} 的 {len(snapshot['bookings'])} 条预约。")


def _update(root, fetch, now, force, clock):
    config = load_config(root)
    if not config["verified"] or not config["room_id"] or config.get("verified_fingerprint") != fingerprint(config):
        return Result("setup_required", "尚未完成首次登录和数据核对；不会访问网站。", 2)
    state = load_json(root / "state.json", {})
    today = now.date().isoformat()
    if state.get("attempt_date") != today:
        state.update(attempt_date=today, attempts=0, next_retry_at=None)
        if state.get("blocked_kind") == "rate_limit":
            state.update(blocked=False, blocked_kind=None)
    # A config change requires setup; past exports from another room never count as today's update.
    if now.time() < time(5) and not force:
        return Result("not_due", "尚未到北京时间 05:00。")
    pending = load_json(root / "pending.json")
    if pending and pending.get("date") == today and pending.get("room_id") == config["room_id"]:
        if pending.get("config_fingerprint") == fingerprint(config):
            return _record_success(root, state, pending, config)
    latest = load_json(root / "latest.json")
    if latest and latest.get("date") == today and latest.get("config_fingerprint") == fingerprint(config):
        # Recover a crash after publishing but before saving success state, without another request.
        if state.get("last_success_at") != latest.get("fetched_at"):
            return _record_success(root, state, latest, config)
    scheduled_time = datetime.combine(now.date(), time(5), CST)
    success_after_five = bool(state.get("last_success_at")) and datetime.fromisoformat(state["last_success_at"]) >= scheduled_time
    if state.get("last_success_date") == today and success_after_five and state.get("last_fingerprint") == fingerprint(config) and not force:
        if not latest or not (root / "查看预约.html").exists():
            raise LocalError("成功记录对应的导出文件缺失，请恢复文件或手动更新。")
        return Result("already_done", "今天已经成功更新，不访问网站。")
    if state.get("blocked"):
        return Result("blocked", "上次发生认证或数据错误，已停止请求；请运行首次配置。", 2)
    if state.get("attempts", 0) >= config["max_attempts_per_day"]:
        return Result("daily_limit", "今天已达到 2 次查询上限，不再请求网站。", 2)
    if now.time() < time(5) and state.get("attempts", 0) >= 1:
        return Result("reserved", "05:00 前最多查询一次，保留一次供当天定时更新。", 2)
    last_attempt = state.get("last_attempt_at")
    if last_attempt:
        since = (now - datetime.fromisoformat(last_attempt)).total_seconds()
        if since < 60 * config["min_interval_minutes"]:
            return Result("cooldown", "距上次请求不足 10 分钟，不访问网站。")
    if state.get("next_retry_at") and now < datetime.fromisoformat(state["next_retry_at"]):
        return Result("cooldown", "尚未到允许重试的时间。")
    # Persist intent BEFORE the network request; a reboot must not reset the request budget.
    state.update(attempts=state.get("attempts", 0) + 1, last_attempt_at=now.isoformat(),
                 next_retry_at=None, status="querying", last_error="上次查询可能被中断")
    write_json(root / "state.json", state)
    try:
        payload = fetch(config, now.date())
        completed = clock().astimezone(CST)
        if completed.date() != now.date():
            raise FetchError("查询跨过午夜，本次不发布旧日期；下次将查询今天。", retryable=True, kind="rollover")
        snapshot = build_snapshot(payload, config, completed)
        state["replay_verified"] = True
        write_json(root / "pending.json", snapshot)
        return _record_success(root, state, snapshot, config)
    except FetchError as exc:
        retry = exc.retryable and state["attempts"] < config["max_attempts_per_day"]
        state.update(last_error=str(exc), status="retry_pending" if retry else "failed",
                     blocked=not exc.retryable, blocked_kind=exc.kind if not exc.retryable else None,
                     next_retry_at=(now + timedelta(minutes=config["retry_minutes"])).isoformat() if retry else None)
        write_json(root / "state.json", state)
        write_bytes(root / "查看预约.html", html_text(latest, str(exc)).encode("utf-8"))
        return Result(state["status"], str(exc), 1 if retry else 2)


def run_update(root, fetch, now=None, force=False, clock=None):
    root = Path(root).resolve()
    clock = clock or ((lambda: now) if now is not None else now_cst)
    now = clock().astimezone(CST)
    try:
        with RunLock(root):
            result = _update(root, fetch, now, force, clock)
            if result.status not in {"already_done", "not_due", "cooldown"}:
                log(root, now, result.status, result.message)
            return result
    except BlockingIOError:
        return Result("busy", "已有任务运行，本次退出。")
    except (OSError, ValueError, TypeError) as exc:
        raise LocalError("本地文件或状态异常；已停止，请查看运行状态。") from exc

"""Map confirmed meetings to the user's piano timetable, preserving same-day manual entries."""
from datetime import date, datetime, timedelta
import hashlib
import json
import re

from . import core
from .feishu import FeishuError, _plain, _text, identifier

LABEL = "会议占用"
SLOTS = {**{row: (510 + (row - 2) * 30, 540 + (row - 2) * 30) for row in range(2, 10)},
         **{row: (840 + (row - 11) * 30, 870 + (row - 11) * 30) for row in range(11, 27)}}
APPROVED = {1, 6, 7, 9}
PENDING = {0, 8}
INACTIVE = {2, 3, 4, 5, 10}


def validate_target(target):
    identifier(target.get("sheet_id"))
    if target.get("conflict_policy") not in {"meeting_wins", "manual_wins"}:
        raise FeishuError("钢琴预约表的冲突处理方式尚未配置。")
    if type(target.get("include_pending")) is not bool:
        raise FeishuError("钢琴预约表的会议状态规则尚未配置。")


def read_grid(client, token, sheet):
    values = client.read_values(token, sheet + "!A1:C26")
    if len(values) > 26 or any(not isinstance(row, list) or len(row) > 3 for row in values):
        raise FeishuError("钢琴预约表读取范围异常，已停止。")
    entries = {}
    for number, times in SLOTS.items():
        row = values[number - 1] if number <= len(values) else []
        label = _plain(row[0]) if row else ""
        match = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})\s*[-—–~～至]\s*(\d{1,2})[:：](\d{2})\s*", label)
        actual = (int(match[1]) * 60 + int(match[2]), int(match[3]) * 60 + int(match[4])) if match else None
        if actual != times:
            raise FeishuError(f"钢琴预约表 A{number} 的时间与已确认布局不符，已停止以免填错行。")
        entries[str(number)] = _plain(row[1]) if len(row) > 1 else ""
    entries["date"] = _plain(values[1][2]) if len(values) > 1 and len(values[1]) > 2 else ""
    return entries


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def grid_date(value):
    if not value.strip():
        return None
    # Feishu returns typed date cells as spreadsheet serial days, including with ToString rendering.
    if re.fullmatch(r"\d{1,6}(?:\.\d+)?", value.strip()):
        serial = float(value)
        if 1 <= serial < 100000:
            return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()
    match = re.fullmatch(r"\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?\s*", value)
    try:
        if match:
            return date(*map(int, match.groups())).isoformat()
    except ValueError:
        pass
    raise FeishuError("Sheet1 的 C2 不是可识别的日期，请使用 YYYY-MM-DD 格式。")


def occupied_rows(snapshot, include_pending=False):
    try:
        day = date.fromisoformat(snapshot["date"])
        midnight = datetime.combine(day, datetime.min.time(), core.CST)
        active = APPROVED | (PENDING if include_pending else set())
        occupied = set()
        for meeting in snapshot["bookings"]:
            state = meeting.get("state_code")
            if type(state) is not int or state not in APPROVED | PENDING | INACTIVE:
                raise FeishuError("存在无法识别状态的学校预约，钢琴预约表保持原样，需核对后再更新。")
            if state not in active:
                continue
            start = datetime.fromisoformat(meeting["start"])
            end = datetime.fromisoformat(meeting["end"])
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise FeishuError("会议时间无效，钢琴预约表保持原样。")
            for row, (low, high) in SLOTS.items():
                if start < midnight + timedelta(minutes=high) and end > midnight + timedelta(minutes=low):
                    occupied.add(str(row))
        return occupied
    except (KeyError, ValueError, TypeError):
        raise FeishuError("预约快照格式异常，钢琴预约表保持原样。") from None


def plan_changes(entries, occupied, state, day, policy):
    # C2 anchors the initial day; after a confirmed update, the local date protects same-day manual edits.
    reset = (state.get("date") or grid_date(entries["date"])) != day
    desired, new_owned, conflicts = {}, set(), []
    for number in map(str, SLOTS):
        current = entries[number]
        value = "" if reset else current
        if not reset and value == LABEL:
            value = ""
        if number in occupied:
            manual = value not in {"", LABEL}
            if manual:
                conflicts.append(number)
            if not manual or policy == "meeting_wins":
                value = LABEL
                new_owned.add(number)
        desired[number] = value
    # Store only a hash of overwritten manual entries: the user explicitly requested no backup.
    changes = {number: {"before_hash": _hash(entries[number]), "after": value}
               for number, value in desired.items() if entries[number] != value}
    return changes, sorted(new_owned, key=int), conflicts, reset


def _groups(numbers):
    group = []
    for number in sorted(map(int, numbers)):
        if group and number != group[-1] + 1:
            yield group
            group = []
        group.append(number)
    if group:
        yield group


def _resume(client, token, sheet, root, state, entries, today):
    pending = state["pending"]
    if pending["date"] != today:
        raise FeishuError("日期已变化，本次不继续写入旧日期预约。", retryable=True)
    todo = []
    for number, change in pending["changes"].items():
        if int(number) not in SLOTS:
            raise FeishuError("本地钢琴预约表恢复记录超出允许范围。")
        if entries[number] == change["after"]:
            continue
        if _hash(entries[number]) != change["before_hash"]:
            raise FeishuError(f"钢琴预约表 B{number} 在中断后被人工修改，已保留现有内容并停止恢复，请核对。")
        todo.append(number)
    date_changed = grid_date(entries["date"]) != today
    if date_changed and _hash(entries["date"]) != pending["date_before_hash"]:
        raise FeishuError("Sheet1 的 C2 在更新中断后被修改，已停止恢复，请核对日期。")
    for group in _groups(todo):
        if core.now_cst().date().isoformat() != today:
            raise FeishuError("日期已变化，已停止写入旧日期预约。", retryable=True)
        values = [[_text(pending["changes"][str(row)]["after"])] for row in group]
        client.write_values(token, f"{sheet}!B{group[0]}:B{group[-1]}", values)
    if date_changed:
        if core.now_cst().date().isoformat() != today:
            raise FeishuError("日期已变化，已停止写入旧日期。", retryable=True)
        client.write_values(token, sheet + "!C2:C2", [[today]])
    actual = read_grid(client, token, sheet) if todo or date_changed else entries
    for number, change in pending["changes"].items():
        if actual[number] != _text(change["after"]):
            raise FeishuError(f"钢琴预约表 B{number} 读回核对未通过，尚未记为成功。", retryable=True)
    if grid_date(actual["date"]) != today:
        raise FeishuError("Sheet1 的 C2 日期读回核对未通过。", retryable=True)
    state.update(date=today, owned_rows=pending["owned_rows"], snapshot_key=pending["snapshot_key"],
                 fetched_at=pending["fetched_at"], verified_at=core.now_cst().isoformat(),
                 occupied_slots=pending["occupied_slots"], conflicts=pending["conflicts"], reset=pending["reset"])
    state.pop("pending")
    core.write_json(root / "timetable-state.json", state)
    return actual


def publish_timetable(client, snapshot, target, root):
    table = target
    validate_target(table)
    token, sheet = identifier(target["spreadsheet_token"]), identifier(table["sheet_id"])
    today = core.now_cst().date().isoformat()
    if snapshot.get("date") != today:
        raise FeishuError("本地预约不是今天的数据，不修改钢琴预约表。")
    occupied = occupied_rows(snapshot, table["include_pending"])
    target_key = hashlib.sha256(json.dumps([token, sheet, table], sort_keys=True).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([snapshot, table], sort_keys=True).encode()).hexdigest()
    state = core.load_json(root / "timetable-state.json", {})
    if state.get("target_key") and state["target_key"] != target_key:
        raise FeishuError("钢琴预约表目标或规则发生变化，需重新核对后绑定，未清除手填预约。")
    entries = read_grid(client, token, sheet)
    if state.get("pending"):
        if state["pending"].get("date") == today:
            entries = _resume(client, token, sheet, root, state, entries, today)
        else:
            # Old-day intent cannot overwrite a new day.
            state.pop("pending")
    if state.get("snapshot_key") != key or grid_date(entries["date"]) != today:
        changes, owned, conflicts, reset = plan_changes(entries, occupied, state, today, table["conflict_policy"])
        state.update(target_key=target_key, pending={"date": today, "snapshot_key": key,
                     "fetched_at": snapshot["fetched_at"], "changes": changes, "owned_rows": owned,
                     "occupied_slots": len(occupied), "conflicts": conflicts, "reset": reset,
                     "date_before_hash": _hash(entries["date"])})
        core.write_json(root / "timetable-state.json", state)
        _resume(client, token, sheet, root, state, entries, today)
    return {"verified": True, "sheet_id": sheet, "date": today, "occupied_slots": state["occupied_slots"],
            "conflict_cells": ["B" + row for row in state["conflicts"]], "reset_previous_day": state["reset"]}

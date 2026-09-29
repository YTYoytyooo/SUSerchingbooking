"""Learn a read-only booking request from the user's normal page view; never probe endpoints."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from urllib.parse import urlsplit

from . import core

READ_PATHS = {
    "/api/SystemApi/GetMeetingsByRoomAndDateAndUser",
    "/api/SystemApi/GetMeetingByMeetingRoomList",
    "/api/SystemApi/GetMeetingCalendar",
    "/api/SystemApi/GetMeetingRoomWeekUesd",
}
SAFE_PARAMS = {"DeptID", "DeptId", "deptid", "MeetingRoomID", "meetingroomid", "MeetingRoomType",
               "MeetingRoomArea", "MeetingRoomBuildID", "Level", "RoomID", "search", "limit", "sort",
               "order", "offset", "page", "StartDate", "EndDate", "StartDateTime", "EndDateTime",
               "currentdate", "date", "UserID", "userid", "States"}
DATE_PARAMS = {"StartDate", "EndDate", "StartDateTime", "EndDateTime", "currentdate", "date"}
TIME_PAIRS = [("StartDateTime", "EndDateTime"), ("StartTime", "EndTime"), ("start", "end")]
KNOWN_EMPTY_LISTS = {"Meetings", "MeetingList", "MeetingRows", "MeetingInfos", "MeetingInfoList", "events"}


def _path_get(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            raise core.FetchError("预约返回结构与首次核对时不同，请重新配置。")
        data = data[key]
    return data


def _lists(node, path=(), depth=0):
    if depth > 5:
        return
    if isinstance(node, list):
        yield list(path), node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from _lists(value, (*path, key), depth + 1)


def _fields(rows):
    first = next((row for row in rows if isinstance(row, dict)), None)
    if not first:
        return None
    for start, end in TIME_PAIRS:
        if isinstance(first.get(start), str) and isinstance(first.get(end), str):
            return {"start": start, "end": end,
                    "title": next((k for k in ["Topical", "MeetingName", "Title", "title"] if k in first), "Topical"),
                    "state": next((k for k in ["State", "state"] if k in first), "State"),
                    "id": next((k for k in ["MeetingID", "id"] if k in first), "MeetingID"),
                    "room": "MeetingRoomID"}
    return None


def learn_profile(path, body, payload, room_id, room_name, day):
    if path not in READ_PATHS or not isinstance(body, dict) or not isinstance(body.get("Data"), dict):
        raise core.LocalError("只能学习已识别的预约只读查询。")
    data = body["Data"]
    if set(data) - SAFE_PARAMS:
        raise core.LocalError("查询包含尚未识别的参数，已停止；不会保存未知字段或猜测用途。")
    if not isinstance(body.get("MessageType"), (int, str)):
        raise core.LocalError("查询缺少 MessageType。")
    template = deepcopy(data)
    bindings = []
    for key in DATE_PARAMS & set(data):
        value = data[key]
        if value not in (None, "") and (not isinstance(value, str) or not value.startswith(day.isoformat())):
            raise core.LocalError("查询包含其他日期，请在网页选择仅今天后再确认。")
        if isinstance(value, str) and value.startswith(day.isoformat()):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d\d:\d\d)?)?", value):
                raise core.LocalError("日期格式尚未识别，已停止。")
            bindings.append({"key": key, "suffix": value[10:]})
            template[key] = "{date}" + value[10:]
    if not bindings:
        raise core.LocalError("没有捕获到今天日期的查询，请在网页选择今天后再确认。")
    rows = core.validate_envelope(payload)
    selector = {"mode": "flat", "path": []}
    fields = _fields(rows)
    scoped = any(str(data.get(k, "")) == room_id for k in ["meetingroomid", "MeetingRoomID"])
    if not fields:
        matching = [r for r in rows if isinstance(r, dict) and str(r.get("MeetingRoomID", "")) == room_id]
        if len(matching) == 1:
            options = []
            for child_path, children in _lists(matching[0]):
                mapping = _fields(children)
                if mapping:
                    options.append((child_path, mapping))
                elif children == [] and child_path and child_path[-1] in KNOWN_EMPTY_LISTS:
                    options.append((child_path, core.default_config()["fields"]))
            if len(options) != 1:
                raise core.LocalError("已找到会议室，但预约列表结构不明确，需核实后才能保存。")
            selector = {"mode": "room", "path": options[0][0]}
            fields = options[0][1]
        elif rows == [] and scoped:
            fields = core.default_config()["fields"]
        else:
            raise core.LocalError("此响应没有明确包含目标会议室的预约列表。")
    if selector["mode"] == "flat" and rows:
        # All records must identify the requested room unless the request itself is narrowly scoped.
        if not scoped and any(str(r.get("MeetingRoomID", "")) != room_id for r in rows):
            raise core.LocalError("此查询包含其他会议室，请在网页筛选目标会议室后再确认。")
    return {"version": 1, "path": path, "message_type": body["MessageType"],
            "data_template": template, "date_bindings": bindings, "selector": selector, "fields": fields}


def validate_profile(profile):
    if not isinstance(profile, dict) or profile.get("path") not in READ_PATHS:
        raise core.LocalError("尚未学习有效预约查询，请运行首次配置。")
    template = profile.get("data_template")
    if not isinstance(template, dict) or set(template) - SAFE_PARAMS:
        raise core.LocalError("已保存的查询参数无效。")
    bindings = profile.get("date_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise core.LocalError("已保存的日期参数无效。")
    for item in bindings:
        if not isinstance(item, dict) or item.get("key") not in DATE_PARAMS:
            raise core.LocalError("查询日期绑定无效。")
        suffix = item.get("suffix")
        if not isinstance(suffix, str) or not re.fullmatch(r"(?:[T ][0-9:.]+(?:Z|[+-]\d\d:\d\d)?)?", suffix):
            raise core.LocalError("查询日期格式无效。")
    if profile.get("selector", {}).get("mode") not in {"flat", "room"}:
        raise core.LocalError("预约列表位置无效。")
    if not isinstance(profile["selector"].get("path"), list):
        raise core.LocalError("预约列表路径无效。")


def query_data(profile, day):
    validate_profile(profile)
    data = deepcopy(profile["data_template"])
    for binding in profile["date_bindings"]:
        data[binding["key"]] = day.isoformat() + binding["suffix"]
    return data


def normalize(payload, config):
    profile = config["source_profile"]
    validate_profile(profile)
    rows = core.validate_envelope(payload)
    if profile["selector"]["mode"] == "room":
        matching = [r for r in rows if isinstance(r, dict) and str(r.get("MeetingRoomID", "")) == config["room_id"]]
        if len(matching) != 1:
            raise core.FetchError("响应中未找到唯一的目标会议室，已保留旧数据。")
        rows = _path_get(matching[0], profile["selector"]["path"])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise core.FetchError("目标会议室的预约列表格式发生变化。")
        if any(row.get("MeetingRoomID") is not None and str(row["MeetingRoomID"]) != config["room_id"] for row in rows):
            raise core.FetchError("目标列表含有其他会议室的预约，已停止更新。")
        rows = [dict(row, MeetingRoomID=config["room_id"]) for row in rows]
    return {"IsSuccess": True, "Data": {"rows": rows}}


class Capture:
    """Only keeps relevant read-only responses in memory, without authentication headers."""
    def __init__(self, room_name):
        self.room_name = room_name
        self.rooms = {}
        self.observations = []
        self.errors = []

    def on_response(self, response):
        url = urlsplit(response.url)
        if url.scheme != "https" or url.netloc != "booking.sustech.edu.cn":
            return
        if url.path not in READ_PATHS | {core.ROOMS_PATH} or response.request.method != "POST":
            return
        try:
            if response.status != 200:
                self.errors.append({"path": url.path, "status": response.status})
                self.errors = self.errors[-10:]
                return
            raw = response.body()
            if len(raw) > 2_000_000:
                return
            payload = json.loads(raw.decode("utf-8-sig"))
            rows = core.validate_envelope(payload)
            for row in rows:
                if isinstance(row, dict) and self.room_name in str(row.get("MeetingRoomName", "")) and row.get("MeetingRoomID"):
                    self.rooms[str(row["MeetingRoomID"])] = str(row["MeetingRoomName"])
            if url.path in READ_PATHS:
                body = response.request.post_data_json
                if isinstance(body, dict) and isinstance(body.get("Data"), dict) and not (set(body["Data"]) - SAFE_PARAMS):
                    self.observations.append({"path": url.path, "body": body, "payload": payload})
                    self.observations = self.observations[-10:]
        except Exception:
            # Do not log response bodies, login tokens or raw browser exceptions.
            self.errors.append({"path": url.path, "status": "unrecognized_response"})
            self.errors = self.errors[-10:]

    def learn(self, day, room_id=None):
        if room_id and room_id not in self.rooms:
            self.rooms[room_id] = self.room_name
        if len(self.rooms) != 1:
            raise core.LocalError("未找到唯一的目标会议室；请在网页用完整名称筛选，再查看今天预约。")
        identifier, name = next(iter(self.rooms.items()))
        last_error = None
        for observation in reversed(self.observations):
            try:
                profile = learn_profile(observation["path"], observation["body"], observation["payload"], identifier, name, day)
                return identifier, name, profile, observation["payload"]
            except core.LocalError as exc:
                last_error = exc
        raise last_error or core.LocalError("尚未观察到成功的当天预约查询；请在网页打开目标会议室今天的预约。")

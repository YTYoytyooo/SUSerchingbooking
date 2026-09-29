"""Bounded Feishu API client and read-back-verified replacement of a dedicated range."""
from datetime import datetime
import hashlib
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import core

ORIGIN = "https://open.feishu.cn"
MARKER = "Bookpiano / 致诚大会议室"


class FeishuError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def operation_name(path):
    if "/auth/" in path:
        return "获取应用凭据"
    if "/wiki/" in path:
        return "解析知识库链接"
    if path.endswith("/sheets/query"):
        return "读取工作表列表"
    if "sheets_batch_update" in path:
        return "创建专用工作表"
    return "访问工作表数据"


def response_error(payload, path, http_status=None):
    """Preserve actionable structured error details without echoing vendor messages or secrets."""
    code = payload.get("code") if isinstance(payload, dict) else None
    label = operation_name(path)
    context = f"{label}失败"
    if http_status is not None:
        context += f"（HTTP {http_status}）"
    if type(code) is int:
        context += f"，飞书错误码 {code}"
    scopes = []
    detail = payload.get("error") if isinstance(payload, dict) else None
    violations = detail.get("permission_violations") if isinstance(detail, dict) else None
    if isinstance(violations, list):
        for item in violations[:20]:
            scope = item.get("subject") if isinstance(item, dict) and item.get("type") == "action_scope_required" else None
            if isinstance(scope, str) and re.fullmatch(r"[a-z][a-z0-9_]*(?::[a-z0-9_.-]+){1,4}", scope):
                scopes.append(scope)
    if scopes:
        return FeishuError(context + "：缺少应用身份接口权限（满足任一项）：" + "、".join(sorted(set(scopes)))
                           + "。请开通后发布应用版本，再重新连接。")
    if code in {99991400, 99991401, 99991402} or http_status == 429:
        return FeishuError(context + "：飞书要求降低请求频率。", retryable=True)
    if http_status is not None and 300 <= http_status < 400:
        return FeishuError(context + "：请求发生重定向，已停止且未转发凭据。")
    if "/auth/" in path:
        advice = "请核对同一应用的 App ID 和 App Secret，并在本机配置窗口重新保存。"
    else:
        advice = "请核对应用已发布、接口权限和目标表格的可编辑授权。"
    return FeishuError(context + "；" + advice, retryable=http_status is not None and http_status >= 500)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise FeishuError("飞书资源编号无效，请重新连接目标表格。")
    return value


class FeishuClient:
    def __init__(self, app_id, app_secret):
        if not isinstance(app_id, str) or not isinstance(app_secret, str) or not app_id or not app_secret:
            raise FeishuError("飞书应用凭据缺失，请在本机配置窗口保存。")
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = None

    def _request(self, method, path, body=None, authenticated=True):
        if not path.startswith("/open-apis/") or any(c in path for c in "\r\n"):
            raise FeishuError("飞书请求地址无效。")
        if authenticated and not self.token:
            auth = self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                                 {"app_id": self.app_id, "app_secret": self.app_secret}, False)
            token = auth.get("tenant_access_token")
            if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
                raise FeishuError("未能取得飞书应用凭据，请核对 App ID 和 App Secret。")
            self.token = token
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(ORIGIN + path, method=method, headers=headers,
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
        try:
            with urllib.request.build_opener(core.NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise FeishuError("飞书响应过大，已停止更新。")
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                except (ValueError, UnicodeError):
                    raise FeishuError("飞书响应格式异常，未确认更新成功。") from None
        except urllib.error.HTTPError as exc:
            try:
                raw_error = exc.read(65537)
                payload = json.loads(raw_error.decode("utf-8")) if len(raw_error) <= 65536 else None
            except (OSError, ValueError, UnicodeError):
                payload = None
            finally:
                exc.close()
            raise response_error(payload, path, exc.code) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            raise FeishuError("飞书连接失败或超时；可能已写入，下次会先读回核对。", retryable=True) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
            raise FeishuError("飞书响应缺少结果代码，已停止。")
        if payload["code"] != 0:
            raise response_error(payload, path)
        return payload

    def _data(self, method, path, body=None):
        data = self._request(method, path, body).get("data")
        if not isinstance(data, dict):
            raise FeishuError("飞书响应缺少数据，未确认操作成功。")
        return data

    def resolve_wiki(self, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".feishu.cn")
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise FeishuError("请使用飞书知识库的 HTTPS 链接。")
        match = re.fullmatch(r"/wiki/([A-Za-z0-9_-]+)/?", parsed.path)
        if not match:
            raise FeishuError("目标应为你提供的 /wiki/ 知识库链接。")
        node = self._data("GET", "/open-apis/wiki/v2/spaces/get_node?token=" + identifier(match[1])).get("node")
        if not isinstance(node, dict):
            raise FeishuError("未能解析知识库节点，请核对应用对节点的访问权限。")
        return node

    def list_sheets(self, token):
        sheets = self._data("GET", f"/open-apis/sheets/v3/spreadsheets/{identifier(token)}/sheets/query").get("sheets")
        if not isinstance(sheets, list) or any(not isinstance(sheet, dict) for sheet in sheets):
            raise FeishuError("飞书工作表列表格式异常。")
        return sheets

    def add_sheet(self, token, title):
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise FeishuError("专用工作表名称无效。")
        data = self._data("POST", f"/open-apis/sheets/v2/spreadsheets/{identifier(token)}/sheets_batch_update",
                          {"requests": [{"addSheet": {"properties": {"title": title}}}]})
        try:
            sheet_id = data["replies"][0]["addSheet"]["properties"]["sheetId"]
        except (KeyError, IndexError, TypeError):
            raise FeishuError("创建工作表的结果未确认，请稍后重新连接；不要手动连续创建。") from None
        matches = [sheet for sheet in self.list_sheets(token) if sheet.get("sheet_id") == sheet_id]
        if len(matches) != 1:
            raise FeishuError("已创建工作表，但未能确认其容量；请稍后重新连接。")
        return matches[0]

    def read_values(self, token, range_name):
        path = f"/open-apis/sheets/v2/spreadsheets/{identifier(token)}/values/" + urllib.parse.quote(range_name, safe="!:")
        data = self._data("GET", path + "?valueRenderOption=ToString")
        value_range = data.get("valueRange")
        if not isinstance(value_range, dict) or "values" not in value_range:
            raise FeishuError("飞书未返回单元格范围，不能将异常响应当作空表。")
        values = value_range["values"]
        if values is None:
            return []
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise FeishuError("飞书单元格数据结构异常。")
        return values

    def write_values(self, token, range_name, values):
        return self._data("PUT", f"/open-apis/sheets/v2/spreadsheets/{identifier(token)}/values",
                          {"valueRange": {"range": range_name, "values": values}})


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (str, int, float)) and not (isinstance(value, float) and not math.isfinite(value)):
        return str(value)
    raise FeishuError("工作表含有无法安全核对的复杂单元格，请使用专用普通工作表。")


def _matrix(values, rows):
    if not isinstance(values, list) or len(values) > rows:
        raise FeishuError("飞书数据超过托管范围。")
    result = []
    for row in values:
        if not isinstance(row, list) or len(row) > 7:
            raise FeishuError("飞书返回了托管范围以外的列。")
        result.append([_plain(cell) for cell in row] + [""] * (7 - len(row)))
    return result + [[""] * 7 for _ in range(rows - len(result))]


def _text(value):
    text = _plain(value)
    if len(text) > 45000:
        raise FeishuError("预约文字超过单元格容量，已保留旧数据。")
    # A zero-width text prefix preserves the displayed title and prevents formula interpretation.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "\u200b" + text
    return text


def snapshot_values(snapshot):
    rows = [[MARKER, "", "", "", "", "", ""],
            ["预约日期", snapshot["date"], "数据获取时间", snapshot["fetched_at"], "预约条数", str(len(snapshot["bookings"])), "北京时间"],
            ["本表为获取时刻的预约快照，请核对日期；后续新增或取消需重新更新。"],
            ["预约日期", "会议室", "开始时间", "结束时间", "活动名称", "预约状态", "数据获取时间"]]
    for booking in snapshot["bookings"]:
        start = datetime.fromisoformat(booking["start"]).astimezone(core.CST)
        end = datetime.fromisoformat(booking["end"]).astimezone(core.CST)
        rows.append([snapshot["date"], snapshot["room_name"], start.strftime("%Y-%m-%d %H:%M"),
                     end.strftime("%Y-%m-%d %H:%M"), booking["title"], booking["state"], snapshot["fetched_at"]])
    if not snapshot["bookings"]:
        rows.append([snapshot["date"], snapshot["room_name"], "", "", "当天暂无预约", "查询成功", snapshot["fetched_at"]])
    return [[_text(cell) for cell in row] for row in rows]


def publish_snapshot(client, snapshot, target, root):
    if target.get("layout") == "timetable":
        from .timetable import publish_timetable
        return publish_timetable(client, snapshot, target, root)
    token, sheet = identifier(target.get("spreadsheet_token")), identifier(target.get("sheet_id"))
    count = target.get("managed_rows")
    if not isinstance(count, int) or not 10 <= count <= 1000:
        raise FeishuError("工作表托管容量无效，请重新连接。")
    values = snapshot_values(snapshot)
    if len(values) > count:
        raise FeishuError("当天预约超过专用工作表容量，请扩充行数并重新连接；未清除旧内容。")
    desired = _matrix(values, count)
    range_name = f"{sheet}!A1:G{count}"
    old = _matrix(client.read_values(token, range_name), count)
    if any(any(row) for row in old) and old[0][0] != MARKER:
        raise FeishuError("目标工作表已有其他内容且缺少工具标记，已停止以免覆盖。")
    digest = hashlib.sha256(json.dumps(desired, ensure_ascii=False).encode("utf-8")).hexdigest()
    if old == desired:
        return {"verified": True, "unchanged": True, "digest": digest, "rows": len(values)}
    backup = root / "feishu-backups" / (core.now_cst().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
    core.write_json(backup, {"target": target, "values": old, "saved_at": core.now_cst().isoformat()})
    # One range update includes today's values and the empty tail. Do not clear yesterday first.
    client.write_values(token, range_name, desired)
    actual = _matrix(client.read_values(token, range_name), count)
    if actual != desired:
        raise FeishuError("飞书读回内容与预期不一致，尚未记为更新完成；旧内容已在本地备份。", retryable=True)
    return {"verified": True, "unchanged": False, "digest": digest, "rows": len(values)}

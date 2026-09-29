"""Verify the user's existing Sheet1 timetable without creating or changing worksheets."""
from datetime import datetime, timedelta

from . import core
from .pipeline import client_from_credentials, settings_fingerprint

def bind_target(client, settings):
    from .feishu import FeishuError
    from .timetable import read_grid, grid_date
    node = client.resolve_wiki(settings["url"])
    if node.get("obj_type") != "sheet" or not node.get("obj_token"):
        raise FeishuError("目标知识库节点不是普通电子表格，请核实链接。")
    token = node["obj_token"]
    title = "Sheet1"
    matches = [sheet for sheet in client.list_sheets(token) if sheet.get("title") == title]
    if len(matches) > 1:
        raise FeishuError("发现多个同名工作表，请先在飞书中区分名称。")
    if not matches:
        raise FeishuError("未找到 Sheet1 工作表，请核对表格名称；程序不会另建工作表。")
    sheet = matches[0]
    identifier = sheet.get("sheet_id")
    if not identifier:
        raise FeishuError("飞书未返回有效工作表编号，本次未写入预约。")
    grid = sheet.get("grid_properties", {})
    rows, columns = grid.get("row_count"), grid.get("column_count")
    if not isinstance(rows, int) or not isinstance(columns, int) or rows < 26 or columns < 3:
        raise FeishuError("Sheet1 必须至少有 26 行、3 列。")
    entries = read_grid(client, token, identifier)
    grid_date(entries["date"])
    return {"layout": "timetable", "spreadsheet_token": token, "sheet_id": identifier,
            "sheet_title": title, "conflict_policy": "meeting_wins", "include_pending": True}


def connect(root):
    from .feishu import FeishuError
    try:
        with core.RunLock(root):
            settings = core.load_json(root / "feishu.json", {})
            if not settings.get("enabled") or not settings.get("url"):
                raise core.LocalError("请先运行“配置账号与飞书”保存本机应用凭据。")
            now = core.now_cst()
            if settings.get("last_connect_attempt") and now - datetime.fromisoformat(settings["last_connect_attempt"]) < timedelta(minutes=10):
                raise core.LocalError("距上次连接验证不足 10 分钟；请完成授权后再试。")
            settings["sheet_title"] = "Sheet1"
            print("目标电子表格：", settings["url"])
            print("目标工作表：", settings.get("sheet_title"))
            print("核对 Sheet1 的时间格、B 列预约区域及 C2 日期；本步骤只读取飞书。")
            settings.update(last_connect_attempt=now.isoformat(), verified=False)
            core.write_json(root / "feishu.json", settings)
            try:
                target = bind_target(client_from_credentials(root), settings)
            except FeishuError as exc:
                settings["last_error"] = str(exc)
                core.write_json(root / "feishu.json", settings)
                raise
            settings.update(target=target, verified=True, verified_at=core.now_cst().isoformat(), last_error="")
            settings["verified_fingerprint"] = settings_fingerprint(settings)
            core.write_json(root / "feishu.json", settings)
            sync = core.load_json(root / "sync-state.json", {})
            sync.update(blocked=False)
            core.write_json(root / "sync-state.json", sync)
            print("Sheet1 已连接。更新时将匹配会议时段填入 B 列、清除前一天全部预约并更新 C2 日期。")
            return 0
    except FeishuError as exc:
        core.log(root, core.now_cst(), "feishu_connect_failed", str(exc))
        raise core.LocalError("飞书连接未完成：" + str(exc)) from None
    except BlockingIOError:
        raise core.LocalError("已有任务运行，请稍后再连接。") from None

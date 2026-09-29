"""One bounded, read-only Feishu check; does not contact the booking website or write cells."""
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bookpiano import core
from bookpiano.feishu import FeishuError
from bookpiano.pipeline import client_from_credentials


def main():
    root = Path(__file__).resolve().parent.parent
    with core.RunLock(root):
        now = core.now_cst()
        path = root / "feishu-diagnostic.json"
        old = core.load_json(path, {})
        if old.get("checked_at") and now - datetime.fromisoformat(old["checked_at"]) < timedelta(minutes=10):
            print("诊断间隔不足 10 分钟，请使用已保存的结果。")
            return 2
        settings = core.load_json(root / "feishu.json", {})
        client = client_from_credentials(root)
        result = {"checked_at": now.isoformat(), "stage": "resolve_wiki", "status": "checking"}
        core.write_json(path, result)
        try:
            node = client.resolve_wiki(settings["url"])
            result["object_type"] = node.get("obj_type")
            if node.get("obj_type") != "sheet":
                raise FeishuError("链接对应的节点不是普通电子表格。")
            result["stage"] = "list_sheets"
            sheets = client.list_sheets(node["obj_token"])
            matches = [sheet for sheet in sheets if sheet.get("title") == settings.get("sheet_title")]
            result.update(sheet_count=len(sheets), matching_sheet_count=len(matches))
            if len(matches) == 1:
                sheet = matches[0]
                rows = sheet.get("grid_properties", {}).get("row_count")
                if type(rows) is not int or rows < 1:
                    raise FeishuError("工作表容量信息缺失。")
                result["stage"] = "read_values"
                client.read_values(node["obj_token"], f"{sheet['sheet_id']}!A1:C26")
            result.update(status="read_checks_passed", error="")
            print("飞书应用凭据、知识库解析、工作表读取检查通过；未创建工作表或写入数据。")
        except FeishuError as exc:
            result.update(status="failed", error=str(exc))
            print(str(exc))
        core.write_json(path, result)
        core.log(root, now, "feishu_diagnostic", result.get("error") or "read_checks_passed")
        return 0 if result["status"] == "read_checks_passed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (core.FetchError, core.LocalError, BlockingIOError) as exc:
        print(str(exc))
        raise SystemExit(2)
    except Exception as exc:
        print("诊断已停止，错误类型：" + type(exc).__name__)
        raise SystemExit(2)

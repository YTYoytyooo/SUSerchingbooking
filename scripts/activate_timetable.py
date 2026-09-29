"""Activate the authorized Sheet1 layout using today's existing export, without querying school."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bookpiano import core
from bookpiano.connect_feishu import connect
from bookpiano.pipeline import run_pipeline


def main():
    root = Path(__file__).resolve().parent.parent
    snapshot = core.load_json(root / "latest.json", {})
    if snapshot.get("date") != core.now_cst().date().isoformat():
        raise core.LocalError("没有今天的本地预约快照，本次不访问学校、不切换表格。")
    result = connect(root)
    if result:
        return result
    def no_school_query(*_):
        raise core.LocalError("本次只允许使用已导出的快照，不发起学校查询。")
    result = run_pipeline(root, no_school_query)
    print(result.message)
    return result.exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (core.FetchError, core.LocalError, BlockingIOError) as exc:
        print(str(exc))
        raise SystemExit(2)
    except Exception as exc:
        print("激活未完成，错误类型：" + type(exc).__name__)
        raise SystemExit(2)

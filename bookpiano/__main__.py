import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import core


def check_ready(root):
    config = core.load_config(root)
    state = core.load_json(root / "state.json", {})
    latest = core.load_json(root / "latest.json", {})
    if not (config["verified"] and config["room_id"] and latest
            and config.get("verified_fingerprint") == core.fingerprint(config)
            and latest.get("config_fingerprint") == core.fingerprint(config)
            and state.get("last_success_at") == latest.get("fetched_at")
            and (root / "查看预约.html").is_file() and not state.get("blocked")):
        raise core.LocalError("尚未完成首次配置、实际数据核对和成功导出，不能启用定时任务。")
    from .credentials import load_secret
    from .pipeline import settings_fingerprint
    from .source import validate_profile
    validate_profile(config.get("source_profile"))
    settings = core.load_json(root / "feishu.json", {})
    sync = core.load_json(root / "sync-state.json", {})
    if not (state.get("replay_verified") and settings.get("verified")
            and settings.get("verified_fingerprint") == settings_fingerprint(settings) and sync.get("status") == "success"
            and sync.get("target_fingerprint") == settings_fingerprint(settings)
            and sync.get("source_fingerprint") == core.fingerprint(config)):
        raise core.LocalError("尚未通过独立预约查询和飞书写入核对，不能启用每日任务。")
    load_secret(root, "school-account")
    load_secret(root, "feishu-app")
    print("学校自动登录配置、预约查询和飞书写入均已有成功记录。本次仅本地检查。")


def demo(root):
    folder = root / "demo"
    folder.mkdir(exist_ok=True)
    config = core.default_config()
    config["room_id"] = "DEMO-NOT-REAL"
    now = core.now_cst()
    day = now.date().isoformat()
    payload = {"IsSuccess": True, "Data": {"rows": [
        {"MeetingID": "sample-1", "MeetingRoomID": config["room_id"], "Topical": "演示：读书分享会",
         "StartDateTime": day + "T09:00:00", "EndDateTime": day + "T10:30:00", "State": 9},
        {"MeetingID": "sample-2", "MeetingRoomID": config["room_id"], "Topical": "演示：小组讨论",
         "StartDateTime": day + "T14:00:00", "EndDateTime": day + "T15:00:00", "State": 0}]}}
    snapshot = core.build_snapshot(payload, config, now)
    snapshot["demo"] = True
    core.write_json(folder / "bookings.json", snapshot)
    core.write_bytes(folder / "bookings.csv", core.csv_text(snapshot).encode("utf-8-sig"))
    core.write_bytes(folder / "演示预约.html", core.html_text(snapshot, demo=True).encode("utf-8"))
    print("已生成独立演示文件；未访问网站、未修改真实更新记录。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="致诚大会议室本地预约导出")
    parser.add_argument("command", choices=["init", "update", "update-local", "status", "demo", "check-ready", "setup", "configure", "connect-feishu"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--force", action="store_true", help="当天已成功也刷新；仍遵守每天两次及十分钟间隔")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "init":
            core.load_config(root)
            if not (root / "查看预约.html").exists():
                core.write_bytes(root / "查看预约.html", core.html_text().encode("utf-8"))
            print("本地目录已准备好，请运行首次配置。")
        elif args.command == "status":
            config = core.load_config(root)
            state = core.load_json(root / "state.json", {})
            print("会议室：", config["room_name"])
            print("首次核对：", "已完成" if config["verified"] else "待完成")
            print("最近成功日期：", state.get("last_success_date", "尚无"))
            print("最近成功时间：", state.get("last_success_at", "尚无"))
            print("最近错误：", state.get("last_error") or "无")
            print("尝试日期 / 次数：", state.get("attempt_date", "尚无"), "/", state.get("attempts", 0))
            print("允许重试时间：", state.get("next_retry_at") or "无")
            sync = core.load_json(root / "sync-state.json", {})
            print("飞书最后成功日期：", sync.get("success_date", "尚无"))
            print("飞书最近错误：", sync.get("last_error") or "无")
            settings = core.load_json(root / "feishu.json", {})
            print("飞书连接错误：", settings.get("last_error") or "无")
            diagnostic = core.load_json(root / "feishu-diagnostic.json", {})
            if diagnostic.get("error"):
                print("最近飞书诊断（" + diagnostic.get("checked_at", "") + "）：", diagnostic["error"])
            print("自动登录每天最多提交一次；验证码或认证失败需要人工处理。")
            print("详情见 logs；定时任务是否安装请查看 Windows 任务计划程序 Bookpiano-*。")
        elif args.command == "demo":
            demo(root)
        elif args.command == "check-ready":
            check_ready(root)
        elif args.command == "setup":
            from .setup import setup
            return setup(root)
        elif args.command == "configure":
            from .configure import configure
            return configure(root)
        elif args.command == "connect-feishu":
            from .connect_feishu import connect
            return connect(root)
        elif args.command in {"update", "update-local"}:
            from .school import fetch_bookings
            def fetch(config, day):
                return fetch_bookings(root, config, day)
            if args.command == "update":
                from .pipeline import run_pipeline
                result = run_pipeline(root, fetch, force=args.force)
            else:
                result = core.run_update(root, fetch, force=args.force)
            print(result.message)
            return result.exit_code
        return 0
    except (core.LocalError, core.FetchError) as exc:
        try:
            core.log(root, core.now_cst(), "local_error", str(exc))
        except OSError:
            pass
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消。")
        return 2
    except Exception as exc:
        # Exceptions from a browser or HTTP client may contain submitted form values. Never print their raw repr.
        code = type(exc).__name__
        try:
            core.log(root, core.now_cst(), "unexpected_error", "未完成操作；错误类型：" + code)
        except OSError:
            pass
        print("操作未完成，已安全停止。错误类型：" + code + "。请查看本地日志。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

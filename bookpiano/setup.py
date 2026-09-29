"""Interactive source verification by observing the actual page, with no API probing."""
from datetime import datetime, timedelta

from . import core
from .school import browser_session, wait_for_enter
from .source import Capture, normalize


def diagnostic(capture):
    result = {"responses": [], "errors": capture.errors, "matched_room_count": len(capture.rooms)}
    for item in capture.observations:
        rows = item["payload"].get("Data", {}).get("rows", [])
        schema = []
        for row in rows[:1]:
            if isinstance(row, dict):
                schema = [{"key": key, "type": type(value).__name__,
                           "item_keys": sorted(value[0].keys()) if isinstance(value, list) and value and isinstance(value[0], dict) else []}
                          for key, value in row.items()]
        result["responses"].append({"path": item["path"], "parameter_names": sorted(item["body"]["Data"]),
                                    "row_count": len(rows), "schema": schema})
    return result


def setup(root):
    try:
        with core.RunLock(root):
            config = core.load_config(root)
            setup_state = core.load_json(root / ".setup_state.json", {})
            now = core.now_cst()
            if setup_state.get("last_started_at") and now - datetime.fromisoformat(setup_state["last_started_at"]) < timedelta(minutes=10):
                raise core.LocalError("距上次打开网站不足 10 分钟，请稍后再运行；本地配置和查看日志不受限制。")
            print("先确保已运行“配置账号与飞书”，本次将使用本机保存的校园账号正常登录。")
            print("登录后请在网页筛选致诚大会议室，选择今天日期，正常查看一次预约。")
            print("程序只观察网页请求，不额外试接口，也不调用上次返回 404 的地址。")
            if input("按 Enter 打开，输入 q 退出：").strip().lower() == "q":
                return 0
            core.write_json(root / ".setup_state.json", {"last_started_at": now.isoformat()})
            capture = Capture(config["room_name"])
            with browser_session(root, interactive=True, capture=capture) as (context, page, token):
                while True:
                    wait_for_enter(page, "请在网页筛选目标会议室并查看今天预约，然后回到此窗口按 Enter：")
                    core.write_json(root / "capture-diagnostic.json", diagnostic(capture))
                    if len(capture.rooms) == 1:
                        room_id, room_name = next(iter(capture.rooms.items()))
                        config.update(room_id=room_id, room_name=room_name, verified=False)
                        core.write_json(root / "config.json", config)
                    try:
                        identifier, name, profile, payload = capture.learn(core.now_cst().date(), config.get("room_id") or None)
                        config.update(room_id=identifier, room_name=name, source_profile=profile, fields=profile["fields"], verified=False)
                        normalized = normalize(payload, config)
                        snapshot = core.build_snapshot(normalized, config, core.now_cst())
                    except (core.LocalError, core.FetchError) as exc:
                        print(str(exc))
                        print("仅保存了接口路径和字段结构到 capture-diagnostic.json，没有保存密码、token 或完整响应。")
                        if input("输入 c 可留在当前浏览器继续核对；其他输入退出（程序不会自动刷新）：").strip().lower() == "c":
                            continue
                        raise core.LocalError("接口尚未确认，浏览器已关闭；可以将诊断文件交给维护者。") from None
                    print("\n观察到的接口：", profile["path"])
                    print("会议室：", name, "预约日期：", snapshot["date"], "预约条数：", len(snapshot["bookings"]))
                    for booking in snapshot["bookings"]:
                        print(booking["start"], "至", booking["end"], booking["title"], booking["state"])
                    if not snapshot["bookings"]:
                        print("接口返回空列表。请确认网页当天确实没有预约。")
                    print("核对时间有无偏移、条数和状态是否一致；本次没有额外发起预约查询。")
                    if input("全部一致输入 y 保存；其他输入退出：").strip().lower() != "y":
                        raise core.LocalError("尚未确认数据一致，未启用定时任务。")
                    config.update(verified=True, verified_at=core.now_cst().isoformat(), verified_fingerprint=core.fingerprint(config))
                    core.write_json(root / "config.json", config)
                    state = core.load_json(root / "state.json", {})
                    state.update(blocked=False, blocked_kind=None, replay_verified=False)
                    core.write_json(root / "pending.json", snapshot)
                    result = core._record_success(root, state, snapshot, config)
                    core.log(root, core.now_cst(), "source_observed", result.message)
                    print(result.message)
                    print("接下来运行“连接飞书表格”，确认专用工作表；最后用“手动更新”完成一次独立查询和写入验证。")
                    return 0
    except BlockingIOError:
        raise core.LocalError("已有更新或配置任务运行，请稍后再试。") from None

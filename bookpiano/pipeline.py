"""Local export and Feishu publication are separate recoverable stages; completion requires both."""
from datetime import datetime, time, timedelta
import hashlib
import json

from . import core
from .credentials import load_secret


def settings_fingerprint(settings):
    payload = {key: settings.get(key) for key in ["url", "target", "credential_revision"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def client_from_credentials(root):
    from .feishu import FeishuClient
    try:
        app = json.loads(load_secret(root, "feishu-app"))
        if not app.get("app_id") or not app.get("app_secret"):
            raise ValueError()
        return FeishuClient(app["app_id"], app["app_secret"])
    except (ValueError, KeyError, TypeError):
        raise core.LocalError("飞书应用凭据尚未正确保存。") from None


def run_pipeline(root, fetch, publisher=None, now=None, force=False, clock=None):
    from .feishu import FeishuError, publish_snapshot
    clock = clock or ((lambda: now) if now is not None else core.now_cst)
    now = clock().astimezone(core.CST)
    try:
        with core.RunLock(root):
            settings = core.load_json(root / "feishu.json", {})
            if (not settings.get("enabled") or not settings.get("verified") or not settings.get("target")
                    or settings.get("verified_fingerprint") != settings_fingerprint(settings)):
                return core.Result("destination_required", "先运行“连接飞书表格”完成目标配置；本次不访问学校。", 2)
            config = core.load_config(root)
            sync = core.load_json(root / "sync-state.json", {})
            today = now.date().isoformat()
            target_hash = settings_fingerprint(settings)
            if sync.get("attempt_date") != today:
                sync.update(attempt_date=today, attempts=0, next_retry_at=None)
            if now.time() < time(5) and not force:
                return core.Result("not_due", "尚未到北京时间 05:00。")
            complete = (sync.get("status") == "success" and sync.get("success_date") == today and sync.get("target_fingerprint") == target_hash
                        and sync.get("source_fingerprint") == core.fingerprint(config)
                        and bool(sync.get("snapshot_fetched_at"))
                        and datetime.fromisoformat(sync["snapshot_fetched_at"]) >= datetime.combine(now.date(), time(5), core.CST))
            if complete and not force:
                return core.Result("already_done", "今天已完成学校数据导出和飞书更新，不访问两端。")
            if sync.get("blocked"):
                return core.Result("sync_blocked", "飞书授权或内容冲突尚未处理，已停止；请检查运行状态。", 2)
            if sync.get("attempts", 0) >= 2:
                return core.Result("sync_daily_limit", "今天已达到飞书更新尝试上限，不再请求。", 2)
            if now.time() < time(5) and sync.get("attempts", 0) >= 1:
                return core.Result("sync_reserved", "05:00 前最多发布一次，保留一次供当天定时更新。", 2)
            if sync.get("next_retry_at") and now < datetime.fromisoformat(sync["next_retry_at"]):
                return core.Result("cooldown", "飞书写入重试间隔不足 10 分钟。")
            state = core.load_json(root / "state.json", {})
            latest = core.load_json(root / "latest.json", {})
            pending_today = (latest.get("date") == today and latest.get("config_fingerprint") == core.fingerprint(config)
                             and not complete and state.get("replay_verified"))
            # A failed cloud write never causes another school query. A page observation needs one independent replay.
            force_source = (force and not pending_today) or not state.get("replay_verified", False)
            result = core._update(root, fetch, now, force_source, clock)
            if result.status not in {"success", "already_done"}:
                if result.status not in {"not_due", "cooldown"}:
                    core.log(root, now, result.status, result.message)
                return result
            snapshot = core.load_json(root / "latest.json")
            if not snapshot or snapshot.get("date") != today or clock().astimezone(core.CST).date().isoformat() != today:
                return core.Result("rollover", "日期已经变化，本次不将旧日期写入飞书。", 2)
            if sync.get("last_attempt_at") and (now - datetime.fromisoformat(sync["last_attempt_at"])).total_seconds() < 600:
                return core.Result("cooldown", "距上次飞书更新不足 10 分钟。")
            sync.update(attempts=sync.get("attempts", 0) + 1, last_attempt_at=now.isoformat(), status="writing",
                        pending_fetched_at=snapshot["fetched_at"], next_retry_at=None)
            core.write_json(root / "sync-state.json", sync)
            try:
                receipt = (publisher or (lambda snapshot, target, root: publish_snapshot(client_from_credentials(root), snapshot, target, root)))(snapshot, settings["target"], root)
                sync.update(status="success", success_date=today, success_at=clock().isoformat(),
                            snapshot_fetched_at=snapshot["fetched_at"], source_fingerprint=core.fingerprint(config),
                            target_fingerprint=target_hash, receipt=receipt, blocked=False, last_error="", next_retry_at=None)
                core.write_json(root / "sync-state.json", sync)
                message = "学校数据已导出，飞书写入并读回核对通过。"
                if settings["target"].get("layout") == "timetable":
                    message = (f"Sheet1 更新成功，标记 {receipt['occupied_slots']} 个会议占用时间格，C2 日期 {today}。"
                               + ("已清除前一天全部预约内容。" if receipt.get("reset_previous_day") else "当天非重叠手填预约保留。"))
                core.log(root, now, "sync_success", message)
                return core.Result("success", message)
            except (FeishuError, core.FetchError, core.LocalError) as exc:
                retryable = bool(getattr(exc, "retryable", False))
                retry = retryable and sync["attempts"] < 2
                sync.update(status="retry_pending" if retry else "failed", last_error=str(exc), blocked=not retryable,
                            next_retry_at=(now + timedelta(minutes=10)).isoformat() if retry else None)
                core.write_json(root / "sync-state.json", sync)
                core.log(root, now, "sync_failed", str(exc))
                return core.Result("sync_retry_pending" if retry else "sync_failed", "本地数据已保存，飞书更新未完成：" + str(exc), 1 if retry else 2)
    except BlockingIOError:
        return core.Result("busy", "已有任务运行，本次退出。")

"""Local-only credential entry; no telemetry, web server, or network requests."""
import json
import uuid
import re
from urllib.parse import urlsplit, urlunsplit
import tkinter as tk
from tkinter import messagebox, ttk

from . import core
from .credentials import load_secret, save_secret

def normalize_target_url(value):
    """Validate a local setting without opening the link or retaining tracking parameters."""
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".feishu.cn")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not re.fullmatch(r"/wiki/[A-Za-z0-9_-]+/?", parsed.path)):
            raise ValueError
        return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise ValueError("请填写飞书普通电子表格的 HTTPS 知识库链接（/wiki/）。") from None


def configure(root):
    window = tk.Tk()
    window.title("会议室自动更新 · 本机配置")
    window.geometry("720x640")
    window.minsize(680, 620)
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="保存学校账号与飞书应用", font=("Microsoft YaHei", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
    ttk.Label(frame, text="敏感信息只写入当前 Windows 用户的凭据管理器。\n本窗口不登录、不查询预约、不写入飞书。留空密码/密钥可保留已存配置。",
              wraplength=590).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
    fields = {}
    definitions = [("username", "校园用户名", False), ("password", "校园密码", True),
                   ("app_id", "飞书 App ID", False), ("app_secret", "飞书 App Secret", True),
                   ("url", "飞书知识库链接", False), ("sheet_title", "目标工作表", False)]
    account, app = {}, {}
    try:
        account = json.loads(load_secret(root, "school-account"))
    except (core.FetchError, ValueError):
        pass
    try:
        app = json.loads(load_secret(root, "feishu-app"))
    except (core.FetchError, ValueError):
        pass
    settings = core.load_json(root / "feishu.json", {})
    defaults = {"username": account.get("username", ""), "app_id": app.get("app_id", ""),
                "url": settings.get("url", ""), "sheet_title": "Sheet1"}
    for row, (name, label, sensitive) in enumerate(definitions, 2):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=9)
        fields[name] = tk.StringVar(value=defaults.get(name, ""))
        ttk.Entry(frame, textvariable=fields[name], show="●" if sensitive else "", width=48,
                  state="readonly" if name == "sheet_title" else "normal").grid(row=row, column=1, sticky="ew", pady=9)
    ttk.Label(frame, text="更新 Sheet1：B2:B9、B11:B26 填入会议占用，C2 显示日期。\n跨天清除上一天全部手填预约；与当天会议重叠的内容直接覆盖，不备份。", wraplength=590).grid(row=8, column=0, columnspan=2, sticky="w", pady=16)
    accepted = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text="同意在本机保存凭据，用于正常登录学校及更新目标飞书表格", variable=accepted).grid(row=9, column=0, columnspan=2, sticky="w")

    def save():
        if not accepted.get():
            messagebox.showinfo("需要确认", "请勾选本机保存和用途确认。", parent=window)
            return
        values = {key: var.get() for key, var in fields.items()}
        try:
            target_url = normalize_target_url(values["url"])
        except ValueError as exc:
            messagebox.showerror("链接无效", str(exc), parent=window)
            return
        new_account = {"username": values["username"].strip(), "password": values["password"] or account.get("password", "")}
        new_app = {"app_id": values["app_id"].strip(), "app_secret": values["app_secret"] or app.get("app_secret", "")}
        if not all(new_account.values()) or not all(new_app.values()) or not values["sheet_title"].strip():
            messagebox.showerror("配置不完整", "请填写学校账号密码、飞书应用凭据和工作表名称。", parent=window)
            return
        try:
            with core.RunLock(root):
                save_secret(root, "school-account", json.dumps(new_account, ensure_ascii=False))
                save_secret(root, "feishu-app", json.dumps(new_app))
                target_changed = (settings.get("url") != target_url
                                  or settings.get("sheet_title") != values["sheet_title"].strip() or new_app != app)
                settings.update(enabled=True, url=target_url, sheet_title=values["sheet_title"].strip(), managed_rows=1000)
                if target_changed:
                    settings.pop("target", None)
                    settings.pop("verified", None)
                    settings["credential_revision"] = uuid.uuid4().hex
                core.write_json(root / "feishu.json", settings)
                if new_account != account:
                    auth = core.load_json(root / "auth-state.json", {})
                    auth["invalid"] = True
                    core.write_json(root / "auth-state.json", auth)
                    config = core.load_config(root)
                    config["verified"] = False
                    core.write_json(root / "config.json", config)
                # Saving corrected credentials clears a permanent stop, but never resets daily request counters.
                state = core.load_json(root / "state.json", {})
                state.update(blocked=False, blocked_kind=None)
                core.write_json(root / "state.json", state)
                sync = core.load_json(root / "sync-state.json", {})
                sync.update(blocked=False)
                core.write_json(root / "sync-state.json", sync)
            for name in ["password", "app_secret"]:
                fields[name].set("")
            messagebox.showinfo("已保存", "本机凭据已保存。下一步运行“首次配置”，确认当前有效预约查询。", parent=window)
            window.destroy()
        except (core.LocalError, core.FetchError, OSError, BlockingIOError):
            messagebox.showerror("保存失败", "无法保存配置，或已有任务运行。请稍后重试；不要把密码粘贴到聊天。", parent=window)

    ttk.Button(frame, text="保存到本机", command=save).grid(row=10, column=0, columnspan=2, sticky="e", pady=20)
    frame.columnconfigure(1, weight=1)
    window.mainloop()
    return 0

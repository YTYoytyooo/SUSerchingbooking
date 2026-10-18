"""Single local console with a resumable setup workflow; never changes proxy settings."""
import json
import os
import subprocess
import sys
from datetime import datetime, time

from . import core
from .credentials import load_secret
from .pipeline import settings_fingerprint
from .source import validate_profile


def credentials_ready(root):
    try:
        for name, keys in [("school-account", ("username", "password")),
                           ("feishu-app", ("app_id", "app_secret"))]:
            value = json.loads(load_secret(root, name))
            if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in keys):
                return False
        settings = core.load_json(root / "feishu.json", {})
        return bool(settings.get("enabled") and settings.get("url"))
    except (core.FetchError, ValueError, TypeError):
        return False


def source_ready(root):
    config = core.load_json(root / "config.json", {})
    state = core.load_json(root / "state.json", {})
    if not config.get("verified") or state.get("blocked"):
        return False
    try:
        validate_profile(config.get("source_profile"))
        return config.get("verified_fingerprint") == core.fingerprint(config)
    except (core.LocalError, core.FetchError, KeyError):
        return False


def destination_ready(root):
    settings = core.load_json(root / "feishu.json", {})
    sync = core.load_json(root / "sync-state.json", {})
    return bool(settings.get("enabled") and settings.get("verified") and settings.get("target")
                and not sync.get("blocked")
                and settings.get("verified_fingerprint") == settings_fingerprint(settings))


def today_complete(root):
    """A cooldown can return exit code zero; require a fresh verified cloud receipt too."""
    now = core.now_cst()
    sync = core.load_json(root / "sync-state.json", {})
    config = core.load_json(root / "config.json", {})
    settings = core.load_json(root / "feishu.json", {})
    try:
        fetched = datetime.fromisoformat(sync["snapshot_fetched_at"])
        return bool(sync.get("status") == "success" and sync.get("success_date") == now.date().isoformat()
                    and fetched.date() == now.date() and fetched >= datetime.combine(now.date(), time(5), core.CST)
                    and sync.get("receipt", {}).get("verified")
                    and sync.get("source_fingerprint") == core.fingerprint(config)
                    and sync.get("target_fingerprint") == settings_fingerprint(settings))
    except (KeyError, ValueError, TypeError):
        return False


def run_process(root, argv):
    return subprocess.run([str(arg) for arg in argv], cwd=root, check=False).returncode


def python_path(root):
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def prepare(root):
    """Only install missing components; normal launches never reinstall dependencies."""
    target = root / ".venv" / "Scripts" / "python.exe"
    if not target.is_file():
        if run_process(root, [sys.executable, "-m", "venv", root / ".venv"]):
            return 2
    if run_process(root, [target, "-c", "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"]) == 0:
        print("登录组件已就绪，跳过安装。")
        return 0
    print("正在准备登录组件；本步骤不访问学校。")
    return run_process(root, [target, "-m", "pip", "install", "--disable-pip-version-check", "--retries", "1",
                              "--timeout", "30", "-r", root / "requirements-login.txt"])


def action(root, command):
    if command == "prepare":
        return prepare(root)
    if command in {"install", "install-if-missing", "remove"}:
        args = ["powershell.exe", "-NoProfile", "-File", root / "scripts" / "tasks.ps1",
                "-PythonPath", python_path(root)]
        if command == "remove":
            args.append("-Remove")
        elif command == "install-if-missing":
            args.append("-IfMissing")
        return run_process(root, args)
    args = [python_path(root), "-m", "bookpiano", command, "--root", root]
    if command == "manual-update":
        args[3] = "update"
        args.append("--force")
    return run_process(root, args)


def wizard(root, runner=None):
    runner = runner or (lambda command: action(root, command))
    steps = [("prepare", "准备登录组件", None),
             ("configure", "保存学校账号和飞书配置", credentials_ready),
             ("setup", "登录学校并核对预约", source_ready),
             ("connect-feishu", "连接飞书表格", destination_ready),
             ("update", "更新今天的预约并核对写入", today_complete),
             ("check-ready", "检查自动更新条件", None),
             ("install-if-missing", "安装自动更新（已安装则保留）", None)]
    print("向导会跳过已完成的配置；关闭配置窗口或任一步失败即停止。")
    print("学校查询、登录和飞书写入仍遵守原有次数及间隔限制，不修改代理设置。")
    for index, (command, label, ready) in enumerate(steps, 1):
        print(f"\n[{index}/{len(steps)}] {label}")
        if ready and ready(root):
            print("已有有效记录，跳过。")
            continue
        code = runner(command)
        if code:
            print(f"已停在“{label}”，后续步骤未执行。处理上方提示后重新选择向导即可。")
            return code
        if ready and not ready(root):
            print(f"“{label}”尚未完成验证，已停止。若提示冷却或次数限制，请到允许时间再继续。")
            return 2
    print("\n配置与当天更新已完成，自动任务已就绪。以后日常只需选择“一键更新”。")
    return 0


def advanced(root):
    choices = {"1": ("configure", "修改账号与飞书配置"), "2": ("setup", "重新登录并核对学校预约"),
               "3": ("connect-feishu", "重新连接飞书"), "4": ("install", "安装或调整自动任务"),
               "5": ("remove", "移除自动任务"), "6": ("prepare", "准备登录组件"), "7": ("demo", "生成离线演示")}
    for key, (_, label) in choices.items():
        print(key + "  " + label)
    print("0  返回")
    choice = input("请选择：").strip()
    if choice in choices:
        return action(root, choices[choice][0])
    return 0


def menu(root):
    while True:
        print("\n=== 致诚大会议室 · 预约管理 ===")
        print("1  首次配置 / 继续未完成的配置（自动衔接）")
        print("2  一键更新今天的预约")
        print("3  查看更新状态")
        print("4  打开日志文件夹")
        print("5  高级维护（账号、登录、飞书、定时任务）")
        print("0  退出")
        try:
            choice = input("请选择：").strip()
            if choice == "0":
                return 0
            if choice == "1":
                wizard(root)
            elif choice == "2":
                action(root, "manual-update")
            elif choice == "3":
                action(root, "status")
            elif choice == "4":
                folder = root / "logs"
                if folder.is_dir():
                    os.startfile(str(folder))
                else:
                    print("尚未生成日志。")
            elif choice == "5":
                advanced(root)
            else:
                print("请输入菜单中的数字。")
                continue
            input("按 Enter 返回主菜单：")
        except (KeyboardInterrupt, EOFError):
            print("\n已退出管理菜单。")
            return 0
        except (core.LocalError, core.FetchError, OSError) as exc:
            print("操作未完成，请检查配置和运行权限。错误类型：" + type(exc).__name__)

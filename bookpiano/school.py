"""Normal CAS login with a saved account. One submission/day; no CAPTCHA solving."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import threading
import time
from urllib.parse import urlsplit

from . import core
from .credentials import load_secret, load_token, save_token

CAS_LOGIN = "https://cas.sustech.edu.cn/cas/login?service=https://booking.sustech.edu.cn/redirect"


def load_account(root):
    try:
        account = json.loads(load_secret(root, "school-account"))
        if not all(isinstance(account.get(k), str) and account[k] for k in ["username", "password"]):
            raise ValueError()
        return account
    except (ValueError, TypeError):
        raise core.FetchError("本机学校账号配置无效，请重新保存。", kind="auth") from None


def login_available(root):
    auth = core.load_json(root / "auth-state.json", {})
    return auth.get("login_date") != core.now_cst().date().isoformat() or auth.get("login_submissions", 0) < 1


def invalidate_session(root):
    state = core.load_json(root / "auth-state.json", {})
    state["invalid"] = True
    core.write_json(root / "auth-state.json", state)


def cached_token(root):
    auth = core.load_json(root / "auth-state.json", {})
    try:
        age = core.now_cst() - datetime.fromisoformat(auth["obtained_at"])
        if not auth.get("invalid") and timedelta(0) <= age < timedelta(hours=6):
            return load_token(root)
    except (KeyError, ValueError, core.FetchError):
        pass
    return None


def context_token(context):
    tokens = [item["value"] for item in context.cookies(core.ORIGIN)
              if item["name"] == "meeting_manager_token" and item["domain"].lstrip(".") == "booking.sustech.edu.cn"]
    return tokens[0] if len(tokens) == 1 else None


def remember_session(root, token):
    save_token(root, token)
    state = core.load_json(root / "auth-state.json", {})
    state.update(obtained_at=core.now_cst().isoformat(), invalid=False)
    core.write_json(root / "auth-state.json", state)


def submit_account_once(root, page):
    url = urlsplit(page.url)
    if (url.scheme != "https" or url.netloc != "cas.sustech.edu.cn"
            or url.path not in {"/cas/login", "/cas/login/"}):
        raise core.FetchError("登录页面地址异常，未填写账号密码。", kind="auth")
    account = load_account(root)
    if not login_available(root):
        raise core.FetchError("今天已经自动提交过一次校园登录，已停止重复登录。", kind="auth")
    page.locator("#fm1 #username").fill(account["username"])
    page.locator("#fm1 #password").fill(account["password"])
    state = core.load_json(root / "auth-state.json", {})
    state.update(login_date=core.now_cst().date().isoformat(), login_submissions=1, invalid=True)
    core.write_json(root / "auth-state.json", state)
    # Observed on the actual public CAS login page. Do not call form.submit() or bypass CAPTCHA JavaScript.
    page.locator('#fm1 button[name="submitBtn"][type="submit"]').click()


@contextmanager
def browser_session(root, interactive=False, capture=None):
    try:
        from playwright.sync_api import sync_playwright, Error as BrowserError
    except ImportError:
        raise core.LocalError("缺少浏览器登录组件，请运行“准备登录组件”。") from None
    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(channel="msedge", headless=not interactive)
            context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
            if capture:
                context.on("response", capture.on_response)
            if not interactive:
                # Login can require GetUserProfile. Block reservation lists and all unrelated booking APIs.
                def auth_only(route):
                    path = urlsplit(route.request.url).path
                    if path != "/api/SystemApi/GetUserProfile":
                        route.abort()
                    else:
                        route.continue_()
                context.route(core.ORIGIN + "/api/**", auth_only)
            page = context.new_page()
            page.goto(CAS_LOGIN, wait_until="domcontentloaded", timeout=30000)
            try:
                page.locator("#fm1 #username").wait_for(state="visible", timeout=5000)
                submit_account_once(root, page)
            except core.FetchError:
                if not interactive:
                    raise
                print("本次未自动填写或提交；请在窗口手动完成正常登录。")
            except BrowserError:
                if not interactive:
                    raise core.FetchError("登录表单发生变化，自动登录已停止。", kind="auth") from None
            deadline = time.monotonic() + (180 if interactive else 35)
            token = None
            while time.monotonic() < deadline:
                token = context_token(context)
                if token:
                    break
                # Pumps browser events only; does not reload the page or poll an HTTP endpoint.
                page.wait_for_timeout(300)
            if not token:
                raise core.FetchError("校园登录未完成，可能需要验证码或账号信息有误；没有再次提交。", kind="auth")
            remember_session(root, token)
            yield context, page, token
        except BrowserError:
            raise core.FetchError("浏览器登录未完成或页面连接失败，未自动重试登录。", kind="auth") from None
        finally:
            if browser:
                browser.close()


def get_token(root):
    token = cached_token(root)
    if token:
        return token
    if not login_available(root):
        raise core.FetchError("今天已尝试自动登录，需要人工处理；已停止后续请求。", kind="auth")
    with browser_session(root) as (_, _, token):
        return token


def fetch_bookings(root, config, day):
    from .source import validate_profile
    validate_profile(config.get("source_profile"))  # Fail before logging in if the old endpoint was never verified.
    token = get_token(root)
    try:
        return core.query_bookings(config, day, token)
    except core.FetchError as exc:
        if exc.kind == "auth":
            invalidate_session(root)
            if login_available(root):
                raise core.FetchError("预约会话已失效，10 分钟后最多正常登录并查询一次。", retryable=True, kind="session") from None
        raise


def wait_for_enter(page, prompt):
    done = threading.Event()
    def read():
        try:
            input(prompt)
        finally:
            done.set()
    threading.Thread(target=read, daemon=True).start()
    while not done.is_set():
        page.wait_for_timeout(200)

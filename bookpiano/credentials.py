"""User-authorized secrets in Windows Credential Manager; nothing secret in config or logs."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path

from .core import FetchError, LocalError


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def target_name(root, name="session"):
    suffix = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:16]
    if name not in {"session", "school-account", "feishu-app"}:
        raise LocalError("未知的凭据用途。")
    return "Bookpiano/booking.sustech.edu.cn/" + suffix + ("" if name == "session" else "/" + name)


def api():
    if os.name != "nt":
        raise LocalError("登录凭据功能需要 Windows；本地测试和演示不受影响。")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(ctypes.POINTER(Credential))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi


def save_secret(root, name, token):
    raw = token.encode("utf-8")
    if not raw or len(raw) > 2560:
        raise LocalError("登录凭据大小异常，未保存。")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = Credential(Type=1, TargetName=target_name(root, name),
                            Comment="Bookpiano local automation credential",
                            CredentialBlobSize=len(raw), CredentialBlob=buffer,
                            Persist=2, UserName="booking-session")
    if not api().CredWriteW(ctypes.byref(credential), 0):
        raise LocalError("无法写入 Windows 凭据管理器。")


def load_secret(root, name):
    advapi = api()
    pointer = ctypes.POINTER(Credential)()
    if not advapi.CredReadW(target_name(root, name), 1, 0, ctypes.byref(pointer)):
        raise FetchError("尚未保存所需凭据，请运行“配置账号与飞书”。", kind="auth")
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob,
                                pointer.contents.CredentialBlobSize).decode("utf-8")
    finally:
        advapi.CredFree(pointer)


def save_token(root, token):
    save_secret(root, "session", token)


def load_token(root):
    return load_secret(root, "session")

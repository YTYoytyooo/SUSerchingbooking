"""Optional local Windows API smoke test using one synthetic, temporary credential."""
import ctypes
from ctypes import wintypes
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bookpiano.credentials import api, load_token, save_token, target_name

with tempfile.TemporaryDirectory(prefix="bookpiano-credential-test-") as directory:
    root = Path(directory)
    token = "SYNTHETIC-TEST-NOT-A-REAL-TOKEN"
    created = False
    try:
        save_token(root, token)
        created = True
        assert load_token(root) == token
        print("Windows Credential Manager synthetic round-trip: OK")
    finally:
        if created:
            library = api()
            library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            library.CredDeleteW.restype = wintypes.BOOL
            if not library.CredDeleteW(target_name(root), 1, 0):
                raise RuntimeError("Could not remove the exact synthetic credential created by this test")

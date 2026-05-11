"""Step 28 — direct exercise of the path-traversal guard in bot._safe_file_path.

We can't easily forge a Telegram callback from a normal client, so we call the
validator the callback handler dispatches to (bot.py:94) with the same
filename strings an attacker could put in the callback_data. A real attack
attempt arrives as `send_<something>`; the handler strips `send_` and hands
the rest to _safe_file_path. We feed that rest directly here.
"""
import os
from bot import _safe_file_path

OUT_DIR = "recordings"

MALICIOUS = [
    "../../windows/system32/config/SAM",   # the exact plan example
    "..\\windows\\system32\\config\\SAM",
    "recording_20260101_000000.mp4/../../../etc/passwd",
    "recording_20260101_000000.mp4\\..\\..\\..\\etc\\passwd",
    "C:/Windows/System32/SAM",
    "/etc/passwd",
    "",
    "recording.mp4",                        # missing timestamp
    "recording_xxxxxxxx_xxxxxx.mp4",        # non-digit
    "recording_20260101_000000.MP4",        # wrong-case extension
    "recording_20260101_000000.mp4.bak",    # trailing junk
    "evil.mp4",
    "..",
    ".",
]

LEGIT = [
    "recording_20260101_000000.mp4",
    "recording_29991231_235959.mp4",
]

fails = 0
print(f"--- malicious (must all return None) ---")
for name in MALICIOUS:
    got = _safe_file_path(name, OUT_DIR)
    ok = got is None
    print(f"  {'PASS' if ok else 'FAIL'}: {name!r:60s} -> {got!r}")
    if not ok:
        fails += 1

print(f"\n--- legit (must return absolute path inside {os.path.abspath(OUT_DIR)}) ---")
base = os.path.abspath(OUT_DIR)
for name in LEGIT:
    got = _safe_file_path(name, OUT_DIR)
    ok = got is not None and os.path.dirname(os.path.abspath(got)) == base
    print(f"  {'PASS' if ok else 'FAIL'}: {name!r:60s} -> {got!r}")
    if not ok:
        fails += 1

print(f"\n{'all guards pass' if fails == 0 else f'{fails} FAILURE(s)'}")
raise SystemExit(0 if fails == 0 else 1)

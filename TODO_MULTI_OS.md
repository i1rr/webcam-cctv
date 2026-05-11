# TODO: Cross-platform support (macOS / Linux)

The project currently targets Windows 10/11 only. The Python application logic (OpenCV, MOG2, the camera state machine, the Telegram bot, asyncio orchestration, config loading) is already portable. The OS coupling is concentrated in three places: sleep prevention, the OpenCV camera-capture backend, and the launcher scripts. This document lists the work needed to make the project run on macOS and Linux without losing the Windows path.

## Scope and non-goals

- **In scope.** Bring up the same functionality on macOS (12+) and Linux (Wayland or X11, USB-class UVC camera) with no behavior changes for Windows users.
- **Out of scope.** Mobile, headless servers without a display (the ROI tool needs a GUI), camera backends other than UVC, packaging into installers, autostart on login.

## Work items

### 1. Abstract sleep prevention behind a platform shim

`windows_utils.py` is a thin wrapper around `SetThreadExecutionState`. Replace it with a `power_utils.py` (or `sleep_inhibit.py`) module that dispatches at import time based on `sys.platform`:

- **Windows** — keep the existing `ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` / `ES_CONTINUOUS` calls.
- **macOS** — spawn `caffeinate -i -w <pid>` as a subprocess in `prevent_sleep()` and store the handle; terminate it in `allow_sleep()`. `-i` blocks idle sleep; `-w <pid>` makes `caffeinate` exit when the main process dies, so a hard crash does not leave the inhibitor running.
- **Linux** — try `systemd-inhibit --what=idle --who=webTelegramCCTV --why="CCTV monitor active" --mode=block sleep infinity` as a subprocess. Fall back to `xdg-screensaver suspend <window>` only if `systemd-inhibit` is missing; on a headless box, log a warning and continue without inhibiting.

Keep the public API exactly `prevent_sleep()` / `allow_sleep()` so `main.py` and the `atexit` registration do not change. Update the import in `main.py` from `from windows_utils import ...` to `from power_utils import ...`.

**Acceptance:** locking the workstation for >30 minutes on each OS does not put the host to sleep; `cctv.log` shows continuous timestamps; on Ctrl+C the inhibitor process is gone (`ps`/`Get-Process` confirms).

### 2. Make the OpenCV camera backend platform-aware

`camera.py:122` calls `cv2.VideoCapture(self.cfg.camera_index, cv2.CAP_DSHOW)`. `CAP_DSHOW` is Windows-only (DirectShow). On other platforms it falls back silently to whatever OpenCV picks but the explicit hint is misleading. Two equally valid options:

- **A. Pick per-platform.** Map `sys.platform` to `CAP_DSHOW` (Windows), `CAP_AVFOUNDATION` (macOS), `CAP_V4L2` (Linux), and use that constant in both `camera.py` and `setup_roi.py:28`.
- **B. Make the backend configurable.** Add `[camera] backend = auto|dshow|avfoundation|v4l2` to `config.ini`, default to `auto`, and use the constant only when the operator overrides. This is more flexible for multi-camera systems on Linux where V4L2 vs GStreamer matters.

Recommend option A for simplicity; option B if you anticipate users with multiple USB hubs or virtual cameras.

Camera enumeration in `TESTING.md` Step 19 also hard-codes `CAP_DSHOW`; update it to use the same per-platform constant, or remove the hint entirely from that snippet.

**Acceptance:** `python setup_roi.py` and `python main.py` open the right device on a Mac with the built-in FaceTime camera, on Linux with a USB UVC webcam, and on Windows with the Brio — no platform-specific code paths outside the constant lookup.

### 3. Add `start_cctv.sh` and `setup_roi.sh`

`start_cctv.bat` and `setup_roi.bat` activate `venv` and run `python <script>`. Add shell equivalents:

- `start_cctv.sh` — `#!/usr/bin/env bash`, `cd "$(dirname "$0")"`, `source venv/bin/activate`, `python main.py`. Make it executable (`chmod +x`).
- `setup_roi.sh` — same shape, runs `python setup_roi.py`.

Leave the `.bat` files alone. Mention in README that Windows users double-click `.bat`, macOS/Linux users run `./start_cctv.sh` from a terminal (no Finder/Nautilus shortcut for the console output).

**Acceptance:** Operator on each OS has a one-command startup with the same console-log behavior as Windows.

### 4. Refactor `SETUP.md` into per-OS sections

`SETUP.md` is entirely Windows power-settings (USB selective suspend, screen saver, Device Manager). Restructure as a top-level "common prerequisites" section (Python, ffmpeg, webcam plugged in, bot token) followed by three OS-specific subsections:

- **Windows** — existing content.
- **macOS** — System Settings → Battery → Options → "Prevent automatic sleeping on power adapter" (or rely on the `caffeinate` inhibitor only); grant Terminal/iTerm camera access at System Settings → Privacy & Security → Camera the first time the script runs (mac will silently fail capture otherwise — a known gotcha to document).
- **Linux** — disable autosuspend on the USB hub if relevant (`echo on > /sys/bus/usb/devices/.../power/control`); confirm `v4l2-ctl --list-devices` finds the camera; install `ffmpeg` via the distro package manager.

Update README's prerequisites table to add a "macOS 12+" / "Linux (X11 or Wayland with `xdg-utils`)" row and link to the new `SETUP.md` subsections.

**Acceptance:** A fresh operator on each OS can complete one-time setup using only `SETUP.md` and arrive at a working `start_cctv.{sh,bat}` run.

### 5. Update `requirements.txt` notes

`opencv-python` and `numpy` wheels for Python 3.13 exist on PyPI for all three OSes, so the pinned requirements should work as-is. Verify on a clean macOS / Linux venv install before claiming this. If a wheel is missing for an arch (e.g., Linux aarch64), document the `pip install --no-binary` fallback or pin a different version.

**Acceptance:** `pip install -r requirements.txt` succeeds on Windows 11, macOS 14 (Apple Silicon), and Ubuntu 24.04 with Python 3.13.

### 6. Update `CLAUDE.md` and `README.md` framing

Once the above lands:

- README intro: drop "Windows desktop CCTV" → "Cross-platform desktop CCTV"; remove the "Developed against a Logitech Brio" Windows-implication where it suggests OS coupling rather than camera coupling.
- CLAUDE.md module map: rename `windows_utils.py` → `power_utils.py` and update the description; note that camera backend selection is platform-aware.
- CLAUDE.md "Working in this repo": replace the sleep-prevention reference (currently implicit-Windows) with a note that the abstraction belongs in `power_utils.py` and per-OS branches go there, not scattered across modules.

**Acceptance:** No file in the repo claims Windows-only support except where genuinely required (`.bat` files, the Windows section of SETUP.md).

## Suggested order

1. Item 1 (sleep abstraction) — small, self-contained, and the most Windows-coupled piece. Doing it first proves the platform shim pattern.
2. Item 2 (camera backend) — one-line change in two files, but needs testing on real hardware per OS.
3. Item 3 (shell launchers) — trivial once items 1–2 work.
4. Item 5 (requirements verification) — can be done in parallel with item 4.
5. Item 4 (SETUP.md) — last, because the per-OS gotchas come out of items 1–3 testing.
6. Item 6 (docs reframe) — cleanup pass after everything else works.

## Risks and known unknowns

- **macOS camera permissions.** First-run prompt may be missed if the script runs from a non-Terminal launcher; `caffeinate` and `cv2.VideoCapture` both need explicit operator approval. May need an early-startup permission check that prints a clear "go grant access in System Settings" message rather than silently failing.
- **Linux Wayland vs X11 for `setup_roi.py`.** OpenCV's HighGUI uses X11 by default; on a pure-Wayland session (e.g., recent Fedora GNOME) the ROI window may not appear or capture mouse events. Document XWayland as a fallback or investigate `cv2.namedWindow` behavior under Wayland before claiming Linux support.
- **`/dev/video*` index instability on Linux.** Hot-plugging or rebooting can shuffle indices. Operators may need a udev rule pinning the Brio to a stable symlink; out of scope to provide the rule, but worth a sentence in SETUP.md Linux section.
- **Codec availability.** `mp4v` fourcc is reliable on Windows via the bundled OpenCV builds. On Linux the wheel sometimes ships without it and falls back to `MJPG` in an `.avi`. The ffmpeg re-encode path on the bot side already handles delivery, but `camera.py`'s `_try_open_writer` should be tested per OS and might need a per-OS fourcc preference list.

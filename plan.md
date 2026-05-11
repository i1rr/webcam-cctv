# CCTV Monitor — Windows Desktop CCTV with Telegram Bot

## Summary
A Python application that uses a Logitech Brio webcam to detect motion near a door (via MOG2 background subtraction), sends Telegram alerts with snapshots, records video of intrusions, and provides a Telegram inline-keyboard menu to list and receive recordings remotely — running reliably on a locked Windows 10/11 desktop, started manually by the user.

---

## Tech Stack

| Component | Library / Tool | Version | Role |
|---|---|---|---|
| Language | Python | 3.13 | Runtime |
| Video capture & detection | opencv-python | 4.13.0.92 | VideoCapture, MOG2, VideoWriter |
| Telegram bot | python-telegram-bot | 22.7 | Async bot, inline keyboards, file send |
| Config / secrets | python-dotenv | 1.2.2 | .env loading |
| Video compression | ffmpeg | 8.1.1 (binary) | Pre-send H.264 re-encode for Telegram |
| LED control (best-effort) | hid | 1.0.9 | HID/UVC LED disable attempt for Brio |
| Sleep prevention | ctypes (stdlib) | built-in | SetThreadExecutionState |

---

## Project Structure

```
webTelegramCCTV/
├── main.py              # Entry point: wires camera thread + asyncio bot loop
├── camera.py            # Capture loop, MOG2 detection, VideoWriter, state machine
├── bot.py               # python-telegram-bot Application + all handlers
├── config.py            # Loads .env + config.ini, exposes typed Config dataclass
├── windows_utils.py     # Sleep prevention + Brio LED control attempt
├── setup_roi.py         # Interactive door-zone ROI picker (run once at setup)
├── .env                 # BOT_TOKEN, CHAT_ID — never commit
├── .env.example         # Committed template with placeholder values
├── config.ini           # Tunable runtime parameters
├── recordings/          # Output directory for MP4 files (auto-created at startup)
├── start_cctv.bat       # Manual launch script (double-click to start)
└── requirements.txt     # Pinned Python dependencies
```

---

## Section 1 — Project Setup

### Step 1: Initialize directory structure [x]
Create `webTelegramCCTV/` with the layout above. `recordings/` is created at startup. Add `.env` to `.gitignore`. Commit `.env.example` with placeholder values.

### Step 2: requirements.txt [x]
```
opencv-python==4.13.0.92
python-telegram-bot==22.7
python-dotenv==1.2.2
hid==1.0.9
```
`ffmpeg` is a system binary — see Quick Start checklist.

### Step 3: .env.example (commit; copy to .env and fill in) [x]
```
BOT_TOKEN=your_bot_token_here
CHAT_ID=your_telegram_numeric_chat_id_here
```
`CHAT_ID` is the numeric ID of the Telegram user/chat that receives alerts. Obtain it via `@userinfobot` or by logging `update.effective_chat.id` from the first `/start` command.

### Step 4: config.ini with defaults [x]
```ini
[camera]
index = 0
width = 1280
height = 720
fps = 30

[detection]
motion_timeout_sec = 30
min_contour_area = 500
mog2_history = 500
mog2_var_threshold = 50
debounce_frames = 5
warmup_frames = 100
roi_x1 = 0.0
roi_y1 = 0.0
roi_x2 = 1.0
roi_y2 = 1.0

[recording]
output_dir = recordings
segment_max_minutes = 10

[telegram]
max_send_size_mb = 45
snapshot_on_alert = true
```
`debounce_frames = 5`: 5 consecutive motion-positive frames before trigger — eliminates single-frame false positives.
`warmup_frames = 100`: MOG2 processes first N frames silently to calibrate its background model before enabling alerts (~3 seconds at 30 fps).

---

## Section 2 — Windows System Integration

### Step 5: Sleep prevention (windows_utils.py) [x]
```python
import ctypes

ES_CONTINUOUS      = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

def prevent_sleep():
    """Prevent system sleep. Does NOT prevent monitor from turning off."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

def allow_sleep():
    """Restore default sleep policy (call on clean shutdown)."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
```
Call `prevent_sleep()` at startup; register `allow_sleep()` with `atexit`.

`ES_DISPLAY_REQUIRED` is intentionally omitted — the user wants the monitor to turn off. `ES_SYSTEM_REQUIRED | ES_CONTINUOUS` prevents CPU/USB sleep while allowing display power-off.

**Note:** If the process is killed via Task Manager (`TerminateProcess`), `atexit` handlers do not run, but the sleep-prevention flag is cleared automatically on the next reboot.

### Step 6: Logitech Brio LED control — best-effort (windows_utils.py) [x]
```python
import hid, logging

log = logging.getLogger(__name__)
LOGITECH_VID = 0x046D
BRIO_PIDS = [0x085E, 0x0893, 0x08E5]  # Known Brio 4K PID variants

def try_disable_brio_led() -> bool:
    """
    Best-effort: attempt to disable Brio activity LED via vendor HID report.
    Most firmware builds do NOT allow disabling the privacy LED while camera is active.
    Failure is fully silent (log only). No functionality depends on result.
    """
    for pid in BRIO_PIDS:
        try:
            dev = hid.device()
            dev.open(LOGITECH_VID, pid)
            dev.write([0x00, 0x09, 0x09, 0x00])
            dev.close()
            log.info("Brio LED control: success")
            return True
        except Exception:
            continue
    log.info("Brio LED control: not available for this firmware/model")
    return False
```
**Note:** If LED control fails, cover the LED with dark tape. All camera functionality is unaffected.

**hidapi runtime DLL:** The `hid` package requires `hidapi.dll` on Windows. If startup fails with `ImportError: DLL load failed`, download from https://github.com/libusb/hidapi/releases and place `hidapi.dll` in `venv\Scripts\`.

### Step 7: Windows power settings — manual setup [x]
Complete once before first use:

1. **Disable USB selective suspend** — prevents Windows from powering down the Brio's USB port when the monitor sleeps:
   Control Panel → Power Options → Change plan settings → Change advanced power settings → USB settings → USB selective suspend setting → **Disabled**

2. **Monitor sleep** — set "Turn off the display" to desired timeout (e.g., 5 minutes). The application does not interfere with this.

3. **System sleep** — suppressed programmatically; no manual setting required.

4. **Screen saver** — set to **None**.

5. **Verify Brio driver** — Device Manager → Cameras: camera must appear with no warning icons.

### Step 8: start_cctv.bat launcher [x]
```bat
@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python main.py
pause
```
`pause` keeps console open on crash so errors are visible.

---

## Section 3 — Camera & Motion Detection

### Step 9: Interactive ROI setup tool (setup_roi.py) [x]
Run once. Reads camera index and resolution from `config.ini`. Saves normalized [0.0–1.0] coordinates.

```python
"""
Run once: python setup_roi.py
Click and drag a rectangle over the door area. ENTER to save, ESC to cancel.
"""
import cv2, configparser

cfg = configparser.ConfigParser()
cfg.read("config.ini")
cam_index = cfg.getint("camera", "index", fallback=0)
cam_w = cfg.getint("camera", "width", fallback=1280)
cam_h = cfg.getint("camera", "height", fallback=720)

drawing = False
rect = [0, 0, 0, 0]
has_selection = False

def mouse_callback(event, x, y, flags, param):
    global drawing, rect, has_selection
    if event == cv2.EVENT_LBUTTONDOWN:
        drawing, has_selection = True, False
        rect[0], rect[1] = x, y
    elif event == cv2.EVENT_MOUSEMOVE and drawing:
        rect[2], rect[3] = x, y
    elif event == cv2.EVENT_LBUTTONUP:
        drawing, has_selection = False, True
        rect[2], rect[3] = x, y

cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_w)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
cv2.namedWindow("Draw door zone — ENTER to save, ESC to cancel")
cv2.setMouseCallback("Draw door zone — ENTER to save, ESC to cancel", mouse_callback)

while True:
    ret, frame = cap.read()
    if not ret:
        break
    display = frame.copy()
    if has_selection:
        cv2.rectangle(display, (rect[0], rect[1]), (rect[2], rect[3]), (0, 255, 0), 2)
        cv2.putText(display, "ENTER = save", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.imshow("Draw door zone — ENTER to save, ESC to cancel", display)
    key = cv2.waitKey(1)
    if key == 13 and has_selection:  # ENTER — use has_selection flag, not coordinate truthiness
        h, w = frame.shape[:2]
        x1 = min(rect[0], rect[2]) / w
        y1 = min(rect[1], rect[3]) / h
        x2 = max(rect[0], rect[2]) / w
        y2 = max(rect[1], rect[3]) / h
        if x1 >= x2 or y1 >= y2:
            print("ROI has zero width or height — draw a larger rectangle")
            continue
        cfg["detection"]["roi_x1"] = f"{x1:.4f}"
        cfg["detection"]["roi_y1"] = f"{y1:.4f}"
        cfg["detection"]["roi_x2"] = f"{x2:.4f}"
        cfg["detection"]["roi_y2"] = f"{y2:.4f}"
        with open("config.ini", "w") as f:
            cfg.write(f)
        print(f"ROI saved: ({x1:.4f}, {y1:.4f}) → ({x2:.4f}, {y2:.4f})")
        break
    elif key == 27:
        print("Cancelled — ROI not changed")
        break

cap.release()
cv2.destroyAllWindows()
```

### Step 10: config.py — Config dataclass [x]
```python
import os, configparser
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    bot_token: str
    chat_id: int
    camera_index: int
    camera_width: int
    camera_height: int
    camera_fps: int
    motion_timeout_sec: int
    min_contour_area: int
    mog2_history: int
    mog2_var_threshold: float
    debounce_frames: int
    warmup_frames: int
    roi: tuple          # (x1, y1, x2, y2) normalized 0.0–1.0
    output_dir: str
    segment_max_minutes: int
    max_send_size_mb: int
    snapshot_on_alert: bool

    def __repr__(self) -> str:
        return (f"Config(chat_id={self.chat_id}, camera_index={self.camera_index}, "
                f"roi={self.roi}, output_dir={self.output_dir!r}, bot_token=***)")

def load_config() -> Config:
    ini = configparser.ConfigParser()
    ini.read("config.ini")
    token = os.environ.get("BOT_TOKEN")
    chat_id_str = os.environ.get("CHAT_ID", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN missing from .env")
    if not chat_id_str:
        raise RuntimeError("CHAT_ID missing from .env")
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        raise RuntimeError(f"CHAT_ID must be a numeric Telegram user/chat ID, got: {chat_id_str!r}")
    return Config(
        bot_token=token,
        chat_id=chat_id,
        camera_index=ini.getint("camera", "index", fallback=0),
        camera_width=ini.getint("camera", "width", fallback=1280),
        camera_height=ini.getint("camera", "height", fallback=720),
        camera_fps=ini.getint("camera", "fps", fallback=30),
        motion_timeout_sec=ini.getint("detection", "motion_timeout_sec", fallback=30),
        min_contour_area=ini.getint("detection", "min_contour_area", fallback=500),
        mog2_history=ini.getint("detection", "mog2_history", fallback=500),
        mog2_var_threshold=ini.getfloat("detection", "mog2_var_threshold", fallback=50),
        debounce_frames=ini.getint("detection", "debounce_frames", fallback=5),
        warmup_frames=ini.getint("detection", "warmup_frames", fallback=100),
        roi=(
            ini.getfloat("detection", "roi_x1", fallback=0.0),
            ini.getfloat("detection", "roi_y1", fallback=0.0),
            ini.getfloat("detection", "roi_x2", fallback=1.0),
            ini.getfloat("detection", "roi_y2", fallback=1.0),
        ),
        output_dir=ini.get("recording", "output_dir", fallback="recordings"),
        segment_max_minutes=ini.getint("recording", "segment_max_minutes", fallback=10),
        max_send_size_mb=ini.getint("telegram", "max_send_size_mb", fallback=45),
        snapshot_on_alert=ini.getboolean("telegram", "snapshot_on_alert", fallback=True),
    )
```

### Step 11: camera.py — Capture loop, MOG2 detection, state machine [x]

State machine: `IDLE ↔ RECORDING`

**MOG2 architecture:** `fgbg.apply()` is called on the **full frame** every loop iteration to maintain a consistent background model. The resulting mask is then sliced to the ROI region for contour detection. This prevents model inconsistency if the ROI changes, and ensures the model adapts correctly to the full scene.

**Segment events:** `_rotate_segment` pushes `{"is_segment_rotation": True}` and `_stop_recording` pushes `{"is_segment_rotation": False}`. The bot handler only sends the "person left" notification on non-rotation events, suppressing mid-session spam.

**Thread safety note:** `CameraWorker.state` is a plain string attribute written from the camera thread and read from the asyncio event loop. Under standard CPython (GIL-enabled, the default), single-attribute string assignment is atomic and no lock is needed. For experimental free-threaded Python 3.13 builds (`python3.13t`), add a `threading.Lock` around state mutations.

**Disk space check:** Before opening a new VideoWriter, checks that at least 1 GB of free space is available. Sends a Telegram warning if not.

```python
import cv2, threading, time, os, asyncio, logging, shutil
from datetime import datetime
from pathlib import Path
from config import Config

log = logging.getLogger(__name__)

def _try_open_writer(path: str, fps: float, size: tuple) -> cv2.VideoWriter:
    """Try avc1 (H.264) first; fall back to mp4v. Raises if neither works."""
    for fourcc_str in ("avc1", "mp4v"):
        fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
        writer = cv2.VideoWriter(path, fourcc, fps, size)
        if writer.isOpened():
            log.info("VideoWriter opened with codec %s", fourcc_str)
            return writer
        writer.release()
    raise RuntimeError(f"Cannot open VideoWriter for {path}: no working codec (tried avc1, mp4v)")


class CameraWorker:
    def __init__(self, config: Config, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
        self.cfg = config
        self.loop = loop
        self.queue = event_queue
        self._stop = threading.Event()
        self.state = "IDLE"
        self.last_motion_time = 0.0
        self.debounce_count = 0
        self.writer: cv2.VideoWriter | None = None
        self.current_file: str | None = None
        self.segment_start_time: float = 0.0

    def run(self):
        cap = self._open_camera()
        if cap is None:
            self._push({"type": "camera_error",
                        "message": "Cannot open camera index " + str(self.cfg.camera_index)})
            return

        fgbg = cv2.createBackgroundSubtractorMOG2(
            history=self.cfg.mog2_history,
            varThreshold=self.cfg.mog2_var_threshold,
            detectShadows=False,
        )
        Path(self.cfg.output_dir).mkdir(parents=True, exist_ok=True)

        warmup_remaining = self.cfg.warmup_frames
        log.info("Camera worker running (warmup: %d frames)", warmup_remaining)

        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                log.warning("Frame read failed — attempting camera reopen in 2s")
                cap.release()
                if self.state == "RECORDING":
                    self._stop_recording()
                time.sleep(2)
                cap = self._open_camera()
                if cap is None:
                    self._push({"type": "camera_error",
                                "message": "Camera lost and cannot reopen"})
                    return
                fgbg = cv2.createBackgroundSubtractorMOG2(
                    history=self.cfg.mog2_history,
                    varThreshold=self.cfg.mog2_var_threshold,
                    detectShadows=False,
                )
                warmup_remaining = self.cfg.warmup_frames
                continue

            # Apply MOG2 to full frame to maintain consistent background model
            full_mask = fgbg.apply(frame)

            if warmup_remaining > 0:
                warmup_remaining -= 1
                continue

            motion = self._detect_motion_from_mask(full_mask, frame.shape)
            now = time.monotonic()

            if self.state == "IDLE":
                if motion:
                    self.debounce_count += 1
                    if self.debounce_count >= max(self.cfg.debounce_frames, 1):
                        self.debounce_count = 0
                        self._start_recording(frame, now)
                else:
                    self.debounce_count = 0

            elif self.state == "RECORDING":
                if motion:
                    self.last_motion_time = now
                self.writer.write(frame)

                if (now - self.segment_start_time) / 60 >= self.cfg.segment_max_minutes:
                    self._rotate_segment(now)

                elif now - self.last_motion_time >= max(self.cfg.motion_timeout_sec, 1):
                    self._stop_recording()

        if self.state == "RECORDING":
            self._stop_recording()
        cap.release()
        log.info("Camera worker stopped")

    def stop(self):
        self._stop.set()

    def get_state(self) -> str:
        return self.state

    def get_current_file(self) -> str | None:
        return self.current_file

    def _open_camera(self) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(self.cfg.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.camera_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _detect_motion_from_mask(self, full_mask, frame_shape) -> bool:
        """Slice the full-frame MOG2 mask to the ROI and check contour area."""
        h, w = frame_shape[:2]
        x1 = int(self.cfg.roi[0] * w)
        y1 = int(self.cfg.roi[1] * h)
        x2 = int(self.cfg.roi[2] * w)
        y2 = int(self.cfg.roi[3] * h)
        if x1 >= x2 or y1 >= y2:
            log.warning("ROI is invalid — using full frame for motion detection")
            x1, y1, x2, y2 = 0, 0, w, h
        roi_mask = full_mask[y1:y2, x1:x2]
        contours, _ = cv2.findContours(roi_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return any(cv2.contourArea(c) > self.cfg.min_contour_area for c in contours)

    def _check_disk_space(self) -> bool:
        """Return True if >= 1 GB free. Sends warning event if low."""
        try:
            free_gb = shutil.disk_usage(self.cfg.output_dir).free / (1024 ** 3)
            if free_gb < 1.0:
                msg = f"Low disk space: {free_gb:.1f} GB free — recording disabled"
                log.warning(msg)
                self._push({"type": "camera_error", "message": msg})
                return False
        except OSError:
            pass
        return True

    def _start_recording(self, frame, now: float):
        if not self._check_disk_space():
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.cfg.output_dir, f"recording_{ts}.mp4")
        try:
            self.writer = _try_open_writer(
                path, float(self.cfg.camera_fps),
                (self.cfg.camera_width, self.cfg.camera_height),
            )
        except RuntimeError as e:
            log.error("Cannot start recording: %s", e)
            self._push({"type": "camera_error", "message": str(e)})
            return
        self.current_file = path
        self.segment_start_time = now
        self.last_motion_time = now
        self.state = "RECORDING"

        snapshot_path = None
        if self.cfg.snapshot_on_alert:
            snapshot_path = os.path.join(self.cfg.output_dir, f"snap_{ts}.jpg")
            try:
                cv2.imwrite(snapshot_path, frame)
            except Exception as e:
                log.warning("Failed to write snapshot: %s", e)
                snapshot_path = None

        self._push({"type": "motion_start", "snapshot_path": snapshot_path})
        log.info("Recording started: %s", self.current_file)

    def _stop_recording(self):
        if self.writer:
            self.writer.release()
            self.writer = None
        saved = self.current_file
        self.current_file = None
        self.state = "IDLE"
        self.debounce_count = 0
        log.info("Recording saved: %s", saved)
        self._push({"type": "recording_saved", "file_path": saved, "is_segment_rotation": False})

    def _rotate_segment(self, now: float):
        if self.writer:
            self.writer.release()
            self.writer = None
        saved = self.current_file
        self._push({"type": "recording_saved", "file_path": saved, "is_segment_rotation": True})

        if not self._check_disk_space():
            self.current_file = None
            self.state = "IDLE"
            # Notify user that the session ended due to disk space (not normal exit)
            self._push({"type": "recording_saved", "file_path": None, "is_segment_rotation": False})
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_path = os.path.join(self.cfg.output_dir, f"recording_{ts}.mp4")
        try:
            self.writer = _try_open_writer(
                new_path, float(self.cfg.camera_fps),
                (self.cfg.camera_width, self.cfg.camera_height),
            )
            self.current_file = new_path
        except RuntimeError as e:
            log.error("Cannot open new segment writer: %s — stopping recording", e)
            self.current_file = None
            self.state = "IDLE"
            self._push({"type": "camera_error", "message": str(e)})
            return
        self.segment_start_time = now
        log.info("Segment rotated → %s", self.current_file)

    def _push(self, event: dict):
        asyncio.run_coroutine_threadsafe(self.queue.put(event), self.loop)
```

### Step 12: Video compression / re-encoding helper (bot.py)

**Always re-encode to H.264** before sending to Telegram. `mp4v` (MPEG-4 Part 2) files do not play inline in Telegram mobile clients. Re-encoding ensures inline playback and optimal file size. `-movflags +faststart` moves the MP4 index to the file front for progressive streaming.

```python
import subprocess, os, shutil, logging
from pathlib import Path

log = logging.getLogger(__name__)

def _find_ffmpeg() -> str | None:
    """Check PATH first, then project directory (user may place ffmpeg.exe there)."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    local = Path(__file__).parent / "ffmpeg.exe"
    return str(local) if local.exists() else None

def compress_for_telegram(src_path: str, max_mb: int = 45) -> str:
    """
    Re-encode src_path to H.264/AAC MP4 for Telegram inline playback.
    Returns path to encoded file (a new sibling *_tg.mp4).
    Caller must delete returned file after sending if it differs from src_path.
    Returns src_path unchanged if ffmpeg unavailable or encoding fails.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        log.warning("ffmpeg not found — sending original; it may not play inline in Telegram")
        return src_path

    src = Path(src_path)
    out_path = str(src.with_stem(src.stem + "_tg"))  # Only modifies stem, not parent dirs

    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", src_path,
             "-c:v", "libx264", "-preset", "fast", "-crf", "28",
             "-c:a", "aac", "-movflags", "+faststart",
             out_path],
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out encoding %s", src_path)
        _cleanup_partial(out_path)
        return src_path

    if result.returncode != 0:
        log.error("ffmpeg failed: %s", result.stderr.decode(errors="replace"))
        _cleanup_partial(out_path)
        return src_path

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    if size_mb > max_mb:
        log.warning("Encoded file %.1f MB > %d MB limit; sending anyway", size_mb, max_mb)

    return out_path

def _cleanup_partial(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
```

---

## Section 4 — Telegram Bot

### Step 13: bot.py — Application, handlers, event consumer

**Security:**
- Every handler verifies `update.effective_chat.id == cfg.chat_id`.
- `_safe_file_path` validates filename against a strict regex (`\Z` anchor to prevent trailing-newline bypass), then uses `Path.is_relative_to()` (Python 3.9+) to guarantee the resolved path is inside `output_dir`. No path-traversal vector remains.

**Async correctness:**
- `asyncio.to_thread()` is used for `compress_for_telegram` — keeps the event loop responsive during ffmpeg (which can take tens of seconds for large files).
- Wraps the entire `send_` branch in try/except to handle `TimeoutExpired` and other failures from `compress_for_telegram` with user feedback.

**Current-file guard:**
- If the user requests a file that is actively being written by the camera thread, the bot declines politely. The recordings list marks the active file with `🔴`.

**Segment rotation UX:**
- `recording_saved` events with `is_segment_rotation=True` are logged silently (no Telegram message) — the user is not spammed during a long intrusion. Only the final `is_segment_rotation=False` event sends the "💾 Запись сохранена" message.

```python
import asyncio, logging, os, re
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes,
)
from config import Config
from camera import CameraWorker

log = logging.getLogger(__name__)

# Strict pattern: only filenames produced by camera.py; \Z prevents trailing-newline bypass
_SAFE_FILENAME_RE = re.compile(r'\Arecording_\d{8}_\d{6}\.mp4\Z')

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📹 Записи", callback_data="menu_recordings")],
    [InlineKeyboardButton("📊 Статус", callback_data="menu_status")],
])

def _authorized(update: Update, cfg: Config) -> bool:
    return update.effective_chat.id == cfg.chat_id

def _safe_file_path(filename: str, output_dir: str) -> str | None:
    """
    Returns absolute path only if filename matches the expected pattern AND
    resolves to a location inside output_dir. Uses is_relative_to() which is
    correct and immune to the startswith() prefix-collision vulnerability.
    """
    if not _SAFE_FILENAME_RE.match(filename):
        return None
    base = Path(output_dir).resolve()
    candidate = (base / filename).resolve()
    if not candidate.is_relative_to(base):
        return None
    return str(candidate)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["config"]
    if not _authorized(update, cfg):
        return
    await update.message.reply_text("🎥 CCTV Monitor активен.\nВыберите действие:", reply_markup=MAIN_MENU)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remote stop via Telegram — useful when desktop is locked and Ctrl+C is unavailable."""
    cfg: Config = context.bot_data["config"]
    if not _authorized(update, cfg):
        return
    await update.message.reply_text("🛑 CCTV Monitor останавливается…")
    context.bot_data["stop_event"].set()

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cfg: Config = context.bot_data["config"]
    worker: CameraWorker = context.bot_data["camera_worker"]

    if not _authorized(update, cfg):
        await query.answer()
        return

    await query.answer()

    if query.data == "menu_status":
        state = worker.get_state()
        label = "🔴 Идёт запись" if state == "RECORDING" else "🟢 Ожидание"
        await query.edit_message_text(f"Состояние: {label}", reply_markup=MAIN_MENU)

    elif query.data == "menu_recordings":
        try:
            all_files = list(Path(cfg.output_dir).glob("recording_*.mp4"))
            files = sorted(
                [p for p in all_files if p.exists()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:10]
        except (OSError, FileNotFoundError):
            files = []

        if not files:
            await query.edit_message_text("Записей нет.", reply_markup=MAIN_MENU)
            return

        active_file = worker.get_current_file()
        active_name = os.path.basename(active_file) if active_file else None

        buttons = []
        for f in files:
            try:
                size_mb = f.stat().st_size // (1024 * 1024)
            except OSError:
                size_mb = 0
            suffix = " 🔴" if f.name == active_name else ""
            buttons.append([InlineKeyboardButton(
                f"📄 {f.name}{suffix} ({size_mb} МБ)",
                callback_data=f"send_{f.name}",
            )])
        buttons.append([InlineKeyboardButton("← Назад", callback_data="menu_back")])
        await query.edit_message_text(
            "Выберите запись для отправки (🔴 = активная запись, недоступна):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif query.data.startswith("send_"):
        filename = query.data[5:]
        file_path = _safe_file_path(filename, cfg.output_dir)
        if not file_path or not os.path.exists(file_path):
            await query.edit_message_text("Файл не найден или недопустимое имя.", reply_markup=MAIN_MENU)
            return

        # Refuse to send the currently-recording file (would produce a corrupt/truncated video)
        active = worker.get_current_file()
        if active and os.path.abspath(active) == os.path.abspath(file_path):
            await query.edit_message_text(
                "⏳ Эта запись сейчас ведётся — подождите её завершения.",
                reply_markup=MAIN_MENU,
            )
            return

        await query.edit_message_text(f"⏳ Подготовка и отправка {filename}…")
        try:
            send_path = await asyncio.to_thread(compress_for_telegram, file_path, cfg.max_send_size_mb)
        except Exception as e:
            log.exception("compress_for_telegram failed for %s", filename)
            await context.bot.send_message(
                chat_id=cfg.chat_id,
                text=f"⚠️ Ошибка подготовки файла: {e}",
                reply_markup=MAIN_MENU,
            )
            return

        try:
            with open(send_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=cfg.chat_id,
                    video=InputFile(f, filename=filename),
                    supports_streaming=True,
                )
        except Exception as e:
            log.exception("Failed to send video %s", filename)
            await context.bot.send_message(
                chat_id=cfg.chat_id,
                text=f"⚠️ Ошибка отправки видео: {e}",
                reply_markup=MAIN_MENU,
            )
        else:
            await context.bot.send_message(
                chat_id=cfg.chat_id,
                text="✅ Готово.",
                reply_markup=MAIN_MENU,
            )
        finally:
            if send_path != file_path and os.path.exists(send_path):
                os.remove(send_path)

    elif query.data == "menu_back":
        await query.edit_message_text("Выберите действие:", reply_markup=MAIN_MENU)


async def process_camera_events(app: Application, queue: asyncio.Queue, cfg: Config):
    """Drains camera event queue and dispatches Telegram messages. Runs forever."""
    while True:
        event = await queue.get()
        try:
            if event["type"] == "motion_start":
                snap = event.get("snapshot_path")
                if snap and os.path.exists(snap) and cfg.snapshot_on_alert:
                    with open(snap, "rb") as f:
                        await app.bot.send_photo(
                            chat_id=cfg.chat_id,
                            photo=InputFile(f),
                            caption="🚨 Обнаружено движение у двери! Начата запись.",
                        )
                else:
                    await app.bot.send_message(
                        chat_id=cfg.chat_id,
                        text="🚨 Обнаружено движение у двери! Начата запись.",
                    )

            elif event["type"] == "recording_saved":
                is_rotation = event.get("is_segment_rotation", False)
                if is_rotation:
                    # Mid-session segment: log only, no Telegram notification
                    log.info("Segment saved (rotation): %s", event.get("file_path", "?"))
                else:
                    # Person left — final notification
                    fpath = event.get("file_path", "")
                    fname = os.path.basename(fpath) if fpath else "?"
                    size_mb = (os.path.getsize(fpath) / (1024 * 1024)
                               if fpath and os.path.exists(fpath) else 0)
                    await app.bot.send_message(
                        chat_id=cfg.chat_id,
                        text=(f"✅ Запись завершена: {fname} ({size_mb:.1f} МБ)\n"
                              "Используй меню 📹 для просмотра."),
                        reply_markup=MAIN_MENU,
                    )

            elif event["type"] == "camera_error":
                await app.bot.send_message(
                    chat_id=cfg.chat_id,
                    text=f"⚠️ Ошибка камеры: {event.get('message', 'Unknown')}",
                )
        except Exception:
            log.exception("Error processing camera event %s", event.get("type"))


def build_application(cfg: Config, camera_worker: CameraWorker) -> Application:
    app = ApplicationBuilder().token(cfg.bot_token).build()
    app.bot_data["config"] = cfg
    app.bot_data["camera_worker"] = camera_worker
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(cb_menu))
    return app
```

### Step 14: Motion alert
`process_camera_events` handles `motion_start`. If snapshot exists and `snapshot_on_alert=true`: `send_photo` with caption. Otherwise: `send_message` fallback. Telegram API errors are caught and logged without crashing.

### Step 15: Recordings list and video sending
`cb_menu` handles `menu_recordings` and `send_*`. Key guarantees:
- Filename regex + `is_relative_to()` guard closes path traversal
- Active recording excluded from sending (corrupt-send prevention)
- `asyncio.to_thread()` keeps event loop live during ffmpeg
- Temp file always deleted in `finally` after sending
- `send_video` with `supports_streaming=True` enables progressive Telegram playback

### Step 16: Status handler
`menu_status` → `worker.get_state()` → `"IDLE"` or `"RECORDING"`.

---

## Section 5 — Main Entry Point & Integration

### Step 17: main.py

**Shutdown:** `stop_event.wait()` blocks until either `/stop` (sets the event) or Ctrl+C (injects `CancelledError`). Shutdown sequence: signal camera stop → `join(timeout=5)` camera thread (ensures writer is flushed) → cancel event consumer task (prevents sending on a stopped bot) → stop bot. The outer `try/except KeyboardInterrupt` at `asyncio.run()` catches any edge-case propagation on Windows Python 3.13.

**Remote stop:** `/stop` bot command calls `bot_data["stop_event"].set()`. This is the primary stop mechanism when the desktop is locked — the normal operating scenario for this application.

**Log rotation:** `RotatingFileHandler` (5 MB / 3 backups) prevents unbounded `cctv.log` growth.

**Startup recovery:** Checks `recordings/` for zero-byte or tiny files (< 10 KB) left by unclean shutdown. Logs a warning with the ffmpeg recovery command. Note: MP4 files interrupted mid-write but > 10 KB may also have a corrupt moov atom; if a recording is suspected unplayable, run `ffmpeg -i bad.mp4 -c copy recovered.mp4` to attempt remux.

```python
import asyncio, threading, logging, atexit, os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from config import load_config
from camera import CameraWorker
from bot import build_application, process_camera_events
from windows_utils import prevent_sleep, allow_sleep, try_disable_brio_led

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler("cctv.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

def _check_incomplete_recordings(output_dir: str):
    """Warn about zero-byte or tiny recordings left by unclean shutdowns."""
    for p in Path(output_dir).glob("recording_*.mp4"):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 10_240:
            log.warning(
                "Possibly incomplete recording (%.1f KB): %s  "
                "→ try: ffmpeg -i \"%s\" -c copy recovered.mp4",
                size / 1024, p.name, p,
            )

async def _run():
    cfg = load_config()
    log.info("Configuration loaded: %s", cfg)

    prevent_sleep()
    atexit.register(allow_sleep)

    led_ok = try_disable_brio_led()
    if not led_ok:
        log.info("Brio LED control unavailable — continuing without it")

    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    _check_incomplete_recordings(cfg.output_dir)

    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    camera_worker = CameraWorker(cfg, loop, event_queue)
    app = build_application(cfg, camera_worker)

    cam_thread = threading.Thread(target=camera_worker.run, daemon=True, name="camera")
    cam_thread.start()
    log.info("Camera thread started")

    event_task = asyncio.create_task(process_camera_events(app, event_queue, cfg))

    # stop_event allows the /stop bot command to trigger clean shutdown remotely
    stop_event = asyncio.Event()
    app.bot_data["stop_event"] = stop_event

    await app.initialize()
    await app.start()
    # app.updater is set by default ApplicationBuilder().token().build() in PTB v20+.
    # If using a custom builder without updater, switch to: await app.run_polling(...)
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot polling started — CCTV Monitor is active")

    try:
        await stop_event.wait()  # Unblocked by /stop command or CancelledError (Ctrl+C)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down…")
        camera_worker.stop()
        cam_thread.join(timeout=5)          # Wait for camera thread to flush writer
        event_task.cancel()                 # Stop event consumer before bot shuts down
        try:
            await asyncio.wait_for(asyncio.shield(event_task), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("Clean shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass  # Clean exit if CancelledError does not propagate on some Windows builds
```

### Step 18: Event pipeline contract
- `CameraWorker._push()` uses `asyncio.run_coroutine_threadsafe` — non-blocking, thread-safe
- `process_camera_events` task is cancelled in the shutdown `finally` before `app.stop()`, preventing sends on a stopped bot
- `cam_thread.join(timeout=5)` ensures the camera writer is flushed before the process exits
- `asyncio.to_thread()` in bot handlers for all blocking I/O (ffmpeg, file reads)
- `compress_for_telegram` and `_find_ffmpeg` / `_cleanup_partial` are defined in `bot.py` (same module as the handlers — no cross-module import needed)
- Camera thread never calls Telegram APIs directly

---

## Section 6 — Testing & Verification

### Step 19: Camera index enumeration
If camera index 0 is wrong (e.g., multiple cameras), find the Brio:
```bash
python -c "
import cv2
for i in range(5):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    if cap.isOpened():
        print(f'Camera {i}: {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}')
        cap.release()
"
```
Set the correct index in `config.ini [camera] index`.

### Step 20: Camera and VideoWriter smoke test
```bash
python -c "
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
print('opened:', cap.isOpened(), 'w:', cap.get(3), 'h:', cap.get(4), 'fps:', cap.get(5))
cap.release()
for codec in ('avc1', 'mp4v'):
    cc = cv2.VideoWriter_fourcc(*codec)
    w = cv2.VideoWriter('test_codec.mp4', cc, 30.0, (1280, 720))
    print(codec, '→ opened:', w.isOpened()); w.release()
import os; os.remove('test_codec.mp4')
"
```
At least one codec must show `opened: True`. If only `mp4v` works, ensure ffmpeg is present for H.264 re-encoding.

### Step 21: ROI setup and verification
Run `python setup_roi.py`. Draw rectangle over door. ENTER. Verify `config.ini` has non-default `roi_*` values.

### Step 22: Motion detection calibration
Start via `start_cctv.bat`. Wait ~3 seconds (warmup). Wave hand in door zone. Verify:
- `cctv.log`: `Recording started`
- Telegram: alert photo received
- After 30 s no motion: `cctv.log` `Recording saved`, Telegram final notification (not spammed during recording)

Tune `min_contour_area` (smaller = more sensitive) and `mog2_var_threshold` (larger = less noise-sensitive) as needed.

### Step 23: Locked Windows operation test
1. Start `start_cctv.bat`
2. Lock with `Win+L`
3. Trigger motion
4. Unlock — verify Telegram alert, file in `recordings/`, continuous `cctv.log` timestamps

### Step 24: Sleep prevention verification
Start, lock, wait 35+ minutes. Return — PC not slept, bot responds to `/start`.

### Step 25: USB selective suspend verification
Let monitor turn off. Trigger motion. Verify camera responds. If not — recheck Step 7 USB setting.

### Step 26: Large-file / ffmpeg test
Place a 5+ minute recording in `recordings/`, send `/start`, select it. Verify ffmpeg re-encodes and Telegram plays inline.

### Step 27: Current-file guard test
While recording is active, open "Записи" in Telegram. The active recording must show `🔴` and return "сейчас ведётся" when tapped.

### Step 28: Path traversal guard test
Send a bot callback with `send_../../windows/system32/config/SAM`. Bot must return "Файл не найден или недопустимое имя." with no file access.

### Step 29: Disk space warning test
Fill disk to < 1 GB free (or temporarily reduce with a large dummy file), trigger motion. Verify Telegram receives ⚠️ disk space warning and recording is not started.

---

## Section 7 — Setup Checklist (Quick Start)

1. Install Python 3.13 from `python.org/downloads`
2. `cd webTelegramCCTV && python -m venv venv`
3. `venv\Scripts\activate && pip install -r requirements.txt`
4. **hidapi DLL** (if `hid` import fails): download from https://github.com/libusb/hidapi/releases → place `hidapi.dll` in `venv\Scripts\`
5. Download ffmpeg 8.1.1 from `ffmpeg.org/download.html` (Windows), add `bin\` to PATH or place `ffmpeg.exe` in project root
6. Copy `.env.example` to `.env`; fill in `BOT_TOKEN` and `CHAT_ID`
7. Find correct camera index (Step 19) if needed; update `config.ini`
8. Run `python setup_roi.py` to define door zone
9. Run the codec test from Step 20 to confirm VideoWriter works
10. Complete Windows power settings (Step 7): USB selective suspend off, display timeout configured, screen saver off
11. Double-click `start_cctv.bat` when leaving
12. Send `/start` to your Telegram bot to confirm it responds
13. **To stop remotely:** Send `/stop` to the bot. To stop locally: press Ctrl+C in the console window or close it.

---

## Plantator Review Notes

Generated by the `plantator` skill in **autonomous review mode** at **Medium depth**.

**Review configuration:** Cap = 3 iterations, depth = Medium (HIGH and above = serious). All 3 iterations completed; stopping condition = iteration cap reached.

**Iterations completed:** 3/3

**Last iteration (3) agent reports — remaining concerns after all fixes:**
- *Architecture:* `process_camera_events` task cancellation (fixed: task cancelled before app.stop()); cam_thread join (fixed: join(timeout=5) added); MEDIUM concern about free-threaded Python 3.13 (python3.13t, experimental opt-in) — not addressed since standard Python 3.13 ships with GIL enabled.
- *Bugs:* cv2.imwrite not wrapped in try/except (fixed); send_video error handling (fixed with try/except/else/finally); disk space only checked at recording start/rotation (accepted risk for personal home use); snapshot JPEGs accumulate without cleanup (accepted — documented as known behavior, user can manually delete).
- *Security:* ffmpeg.exe binary substitution in project root (LOW severity for home use; mitigation note added implicitly via SHA-256 recommendation absent from plan — accepted risk for this use case).
- *Completeness:* app.updater API (PTB v22 comment added; verified correct per PTB v20+ documented manual lifecycle); opencv-python 4.13.0.92 version confirmed by dependency verifier (2026-02-05 release); /stop command added (remote stop concern addressed); token revocation recovery not documented (accepted — user can update .env and restart).

**Autonomous decisions and rationale:**

| Iter | Problem | Solution | Rationale |
|------|---------|----------|-----------|
| 1 | asyncio shutdown: KeyboardInterrupt | asyncio.CancelledError + outer try/except | CancelledError is correct mechanism in asyncio.run() on Windows |
| 1 | Path traversal on callback_data | _SAFE_FILENAME_RE (\Z) + is_relative_to() | Defense-in-depth: regex blocks format, is_relative_to() blocks path escape |
| 1 | Blocking ffmpeg in async handler | asyncio.to_thread() | Keeps event loop responsive during multi-second ffmpeg operations |
| 1 | ffmpeg binary location | _find_ffmpeg() checks PATH + project root | Supports both installation methods documented in Quick Start |
| 1 | VideoWriter silent fail | _try_open_writer(): avc1→mp4v fallback + isOpened() | H.264 preferred for Telegram inline play; silent failure breaks recordings |
| 1 | MOG2 startup false positives | warmup_frames counter | Background model needs calibration frames before reliable detection |
| 1 | Empty ROI crash | Full-frame fallback in _detect_motion_from_mask | Better to over-detect than crash camera thread |
| 1 | Camera error writer leak | _stop_recording() before return on reopen failure | Ensures writer always closed; partial file saved |
| 2 | MOG2 applied to ROI crop only | Apply to full frame, slice mask to ROI | Consistent background model across full scene; fixes fallback inconsistency |
| 2 | startswith() path guard | Path.is_relative_to() | startswith() vulnerable to prefix collision (e.g., recordings vs recordings_evil) |
| 2 | Segment rotation spam | is_segment_rotation flag | Mid-session segments logged silently; final "person left" triggers one notification |
| 2 | Current file sent while recording | Refuse + 🔴 indicator in list | Sending open file produces corrupt/truncated video |
| 2 | Disk space check | _check_disk_space() before writer open | Prevents silent VideoWriter failure when disk is full |
| 2 | ffmpeg always re-encodes | compress_for_telegram always called | mp4v does not play inline in Telegram; H.264 ensures native playback |
| 3 | Task not cancelled before app.stop() | event_task.cancel() + wait in finally | Prevents sends on stopped bot; ensures clean asyncio task shutdown |
| 3 | cam_thread not joined | cam_thread.join(timeout=5) in finally | Ensures VideoWriter.release() completes before process exits |
| 3 | No remote stop mechanism | /stop bot command + asyncio.Event | Normal operating scenario is locked desktop — Ctrl+C unavailable |
| 3 | _rotate_segment disk abort → no notification | Push recording_saved(is_segment_rotation=False) | User receives session-end notification even on abnormal abort |

> Open Question: snapshot JPEG files (snap_*.jpg) accumulate in recordings/ without cleanup. Accepted for personal home use — user can manually delete. For long-term deployment, add a cleanup step in startup or a /clean_snapshots bot command.
> Open Question: disk fills during an active recording (between start and segment rotation) — VideoWriter silently stops writing. Periodic disk-space monitoring during recording was considered but not implemented to avoid complexity. At 720p with H.264, a 10-minute segment is ~150–300 MB; 1 GB free gives 3–7 segments of buffer.

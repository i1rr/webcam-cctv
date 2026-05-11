# webTelegramCCTV

Windows desktop CCTV with a USB webcam and a Telegram bot for motion alerts and on-demand recording playback. Developed against a Logitech Brio, but any DirectShow-compatible camera works.

The application watches a configurable region of the camera frame for motion, records segmented MP4 clips while motion persists, sends a snapshot + alert to a single authorized Telegram chat when recording starts, and a summary when it ends. Past recordings are browsable and re-sendable from the bot's inline menu.

Single-host, single-user. Operator starts the application manually via `start_cctv.bat`; the Telegram bot is the only remote interface.

---

## Prerequisites

- Windows 10/11
- **Python 3.13** (the project pins `opencv-python` and `numpy` wheels for this version)
- A USB webcam (developed against Logitech Brio; any DirectShow-compatible camera works)
- A Telegram account
- **ffmpeg** (`ffmpeg.exe` on `PATH` or dropped next to `main.py`) — required to re-encode recordings to H.264 so Telegram plays them inline rather than as documents

---

## One-time setup

### 1. Clone and create the virtual environment

```powershell
git clone <repo-url> webTelegramCCTV
cd webTelegramCCTV
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Install ffmpeg

Either:
- Install ffmpeg system-wide and ensure `ffmpeg.exe` is on `PATH`, **or**
- Download a Windows build from https://ffmpeg.org/download.html and place `ffmpeg.exe` directly in the project root (next to `main.py`). The bot probes both locations.

Without ffmpeg, large recordings are sent as raw files and may not play inline in Telegram.

### 3. Create the Telegram bot

1. In Telegram, open a chat with **@BotFather** and run `/newbot`. Follow the prompts; save the bot token it returns.
2. Open a chat with **@userinfobot** and send any message. It replies with your numeric Telegram user ID.
3. Send any message to your new bot at least once — Telegram will not deliver bot messages to a user who has never initiated contact.

### 4. Configure credentials

Copy `.env.example` to `.env` and fill in:

```
BOT_TOKEN=123456:ABC...                  # from @BotFather
CHAT_ID=987654321                        # numeric ID from @userinfobot
```

`.env` is gitignored — never commit it. `CHAT_ID` must be the numeric ID, not a username; only that single chat is authorized to talk to the bot.

### 5. Run the ROI (region of interest) selector

```powershell
python setup_roi.py
```

A window opens with the live camera feed. Drag a rectangle over the area you want to monitor (e.g., a door), then press **ENTER**. Normalized coordinates are written back to `config.ini` under `[detection]` as `roi_x1`, `roi_y1`, `roi_x2`, `roi_y2`.

### 6. Complete the Windows power-settings checklist

Work through [SETUP.md](SETUP.md). One-time manual settings:

- Disable USB selective suspend (keeps the camera awake when the monitor sleeps)
- Set monitor sleep to your preferred timeout
- Set screen saver to **None**
- Verify the camera appears clean in Device Manager

System sleep is suppressed programmatically by the app while it runs — no manual setting needed.

### 7. Verify

Run through [TESTING.md](TESTING.md) at least once: camera enumeration, codec smoke test, motion calibration, locked-session operation, large-file re-encode, current-file guard, path-traversal guard, and the disk-space warning. Real regressions only show up on real hardware.

---

## Daily use

### Starting the monitor

Double-click `start_cctv.bat` (or run it from a terminal). The script activates `venv` and launches `python main.py`. The console window shows live logs; the same lines are appended to `cctv.log` (rotated at 5 MB, 3 backups kept).

In Telegram, send `/start` to your bot to confirm it is reachable. You should see:

> 🎥 CCTV Monitor active.
> Choose an action:
>
> [📹 Recordings] [📊 Status]

### What the bot sends on its own

| Event                          | Message                                                                  |
| ------------------------------ | ------------------------------------------------------------------------ |
| Motion starts                  | Snapshot photo + caption "🚨 Motion detected at the door! Recording started." |
| Recording ends (no motion 30s) | "✅ Recording finished: `recording_…mp4` (N MB)" + menu                  |
| Segment rolls over mid-event   | Logged only (no Telegram spam during long events)                        |
| Disk free < `min_free_gb`      | "⚠️ Low disk space…" — recording is blocked until space frees            |
| Camera error                   | "⚠️ Camera error: …"                                                     |

The mid-event segment rotation cap is `segment_max_minutes` (default 10). The disk-space warning fires once per OK→low transition, not on every frame.

### Browsing past recordings

Tap **📹 Recordings** in the inline menu. You get up to 10 most-recent files, newest first, with size in MB. Tap any entry — the bot re-encodes it to H.264/AAC via ffmpeg if needed and sends it back as inline-playable video.

- The currently-recording file is marked with **🔴** and refuses to send (it would be truncated). Wait for the recording to end.
- Filenames outside the expected `recording_YYYYMMDD_HHMMSS.mp4` pattern, or paths that escape the recordings directory, are rejected with "File not found or invalid name."

### Checking state

Tap **📊 Status** — shows either "🟢 Idle" or "🔴 Recording".

### Stopping the monitor

Three options, in order of preference:

1. **Telegram `/stop`** — graceful shutdown from anywhere, including when the workstation is locked.
2. **Ctrl+C** in the console window.
3. **Close the console window** — works but is the bluntest option; sleep suppression is still released via `atexit`.

All three perform the same ordered shutdown: stop the camera thread (final segment is flushed), cancel the event consumer, then stop and shut down the Telegram application.

---

## Configuration reference

`config.ini` is checked in with safe defaults. Edit and restart the app to apply.

| Section / key                          | Default     | Notes                                                            |
| -------------------------------------- | ----------- | ---------------------------------------------------------------- |
| `[camera] index`                       | `0`         | DirectShow camera index. If wrong, see TESTING Step 19.          |
| `[camera] width` / `height` / `fps`    | 1280x720@30 | Capture resolution and frame rate.                               |
| `[detection] motion_timeout_sec`       | `30`        | Seconds of no motion before recording is closed.                 |
| `[detection] min_contour_area`         | `500`       | Lower = more sensitive.                                          |
| `[detection] mog2_var_threshold`       | `50`        | Higher = less noise-sensitive.                                   |
| `[detection] debounce_frames`          | `5`         | Frames of consistent motion before triggering IDLE→RECORDING.    |
| `[detection] warmup_frames`            | `100`       | Frames the MOG2 model learns the background before reacting.     |
| `[detection] roi_x1..roi_y2`           | full frame  | Set by `setup_roi.py`. Normalized 0.0–1.0.                       |
| `[recording] output_dir`               | `recordings` | Where MP4s and snapshots are written.                           |
| `[recording] segment_max_minutes`      | `10`        | Hard cap per file; the next segment continues automatically.     |
| `[recording] min_free_gb`              | `1.0`       | Below this, new recordings are refused (existing files kept).    |
| `[telegram] max_send_size_mb`          | `45`        | Above this, ffmpeg re-encode is mandatory.                       |
| `[telegram] snapshot_on_alert`         | `true`      | Send a photo with the motion-start alert.                        |

`BOT_TOKEN` and `CHAT_ID` live in `.env`, never in `config.ini`.

---

## Files and directories produced at runtime

| Path                                       | Purpose                                                            |
| ------------------------------------------ | ------------------------------------------------------------------ |
| `recordings/recording_YYYYMMDD_HHMMSS.mp4` | Motion-triggered video segments.                                   |
| `recordings/snap_YYYYMMDD_HHMMSS.jpg`      | Snapshot sent with the motion-start alert.                         |
| `recordings/*_tg.mp4`                      | Temporary H.264 re-encodes built on demand. Deleted after sending. |
| `cctv.log` (+ `.1`, `.2`, `.3`)            | Rotating application log, 5 MB per file, 3 backups.                |

All four are gitignored. Recordings are **never** auto-deleted — the operator decides when to clear `recordings/`. The disk-space guard refuses new recordings below `min_free_gb` but never deletes existing files.

---

## Module map

- `main.py` — orchestration: loads config, configures logging, prevents sleep, starts the camera thread, runs the bot, awaits a stop event, performs ordered shutdown.
- `camera.py` — capture loop, MOG2 motion detection, debounced state machine, segmented MP4 writing, snapshots, disk-space guard.
- `bot.py` — `python-telegram-bot` Application: `/start`, `/stop`, inline-menu callbacks, ffmpeg re-encode for oversized files, current-file and path-traversal guards.
- `config.py` — `Config` dataclass; reads `config.ini` + `.env`.
- `windows_utils.py` — `SetThreadExecutionState` wrapper to suppress sleep.
- `setup_roi.py` — interactive OpenCV ROI selector; writes back to `config.ini`.
- `start_cctv.bat` — Windows launcher: activates `venv` and runs `python main.py`.
- `SETUP.md` — one-time Windows power-settings checklist.
- `TESTING.md` — operator verification procedures.

---

## Troubleshooting

- **Bot does not respond to `/start`.** Check `cctv.log` for "Bot polling started". Verify `BOT_TOKEN` in `.env` and that you have sent at least one message to the bot first.
- **Bot is unresponsive to your messages but `/start` works for someone else.** Your `CHAT_ID` does not match the one in `.env`. The whitelist is the only authorization layer.
- **Camera not found / wrong feed.** Multiple cameras present — find the right index with the snippet in TESTING Step 19 and set `[camera] index`.
- **Telegram receives a `.mp4` that won't play inline.** ffmpeg is missing. Drop `ffmpeg.exe` next to `main.py` or add it to `PATH`, then resend.
- **Sent file is truncated / 0 bytes.** You tried to send the currently-recording file (🔴). Wait for the segment to close, or use Telegram `/stop` to end it cleanly.
- **No alerts after monitor sleep.** USB selective suspend is still enabled — recheck SETUP step 1.
- **Tiny `recording_*.mp4` files at startup.** Unclean shutdown left a partial segment. `main.py` logs an `ffmpeg -i ... -c copy recovered.mp4` recovery hint per file.

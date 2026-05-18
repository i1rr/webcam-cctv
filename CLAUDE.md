# CLAUDE.md — webTelegramCCTV

Windows desktop CCTV using a Logitech Brio + Telegram bot. Single-host, single-user. The app is designed to run continuously (manually started or auto-started at logon); the bot sends motion alerts, serves recordings on demand, and provides a remote on/off toggle for the camera so the operator can keep the process always-on but release the USB device when not needed.

## Module map

- `main.py` — orchestration. Loads `Config`, sets up rotating file + console logging, prevents sleep, starts `CameraWorker` thread, runs the bot, awaits a stop event, then performs ordered shutdown.
- `camera.py` — capture loop, MOG2 motion detection with 100-frame warmup, debounced IDLE↔RECORDING state machine, segmented mp4 writing (10 min cap), snapshot capture, disk-space guard. Emits events to an `asyncio.Queue` consumed by `bot.py`. Three zone *lists* are applied to the MOG2 mask, each a semicolon-separated `x1,y1,x2,y2` set in `[detection]`: **activation_zones** (any-hit gates IDLE→RECORDING), **sustain_zones** (any-hit gates the keep-alive check during RECORDING), **ignore_zones** (zeroed in the mask before either check). The loader falls back to the legacy single-rect `roi_*` / `sustain_*` keys when the corresponding list key is empty. Worker exposes `enable()` / `disable()` / `is_enabled()` so the bot can release/reacquire the USB device at runtime — the capture handle is opened only inside an inner session, so a disabled worker doesn't hold the camera (Brio LED goes dark, other apps can use it). Default state is enabled so the app starts capturing immediately as before.
- `bot.py` — `python-telegram-bot` Application. `/start` (inline menu with camera on/off toggle at top), `/stop` (remote shutdown), `/status`, callback handlers for listing/sending recordings. Path-traversal guard on filenames, current-file guard so the in-progress segment is never sent, ffmpeg re-encode to H.264 when a file exceeds `max_send_size_mb`. The main menu is built per-request from `worker.is_enabled()` via `_main_menu(worker)` so the toggle button label always matches what tapping will do.
- `config.py` — `Config` dataclass; loads `config.ini` via configparser and `.env` via python-dotenv.
- `windows_utils.py` — `prevent_sleep()` / `allow_sleep()` wrappers around `SetThreadExecutionState`.
- `setup_roi.py` — interactive OpenCV tool. Multi-mode: `1`/`2`/`3` switches between activation / sustain / ignore lists; ENTER appends the drawn rect to the active list; `D` removes the last rect in the active list; `S` writes all three lists to `config.ini [detection]` (and strips the legacy `roi_*` / `sustain_*` single-rect keys); ESC cancels without saving.
- `start_cctv.bat` — venv activate + `python main.py` + `pause`.

## Critical invariants

- **Camera thread never calls Telegram.** All Telegram I/O happens in the asyncio loop in `bot.py`. The camera pushes plain dict events via `asyncio.run_coroutine_threadsafe` (see `camera.py` `_push`).
- **Shutdown order** in `main.py`: stop camera → `cam_thread.join(timeout=5)` → cancel event consumer task → `app.stop()`/`app.shutdown()`. Anything else risks losing the last segment or hanging on exit.
- **CHAT_ID whitelist** is the only authorization. Every handler in `bot.py` checks `update.effective_chat.id == cfg.chat_id` before doing anything. Don't add a handler without that check.
- **Camera toggle releases the device.** `worker.disable()` doesn't pause-in-place — it makes the worker exit its capture session entirely, releasing the OpenCV `VideoCapture` so the USB device is free. Any in-progress recording is finalized in `_run_capture_session`'s `finally`, so the last segment is still flushed and auto-sent. Don't change this to a "skip frames" pause; the whole point is that the camera light goes off and other apps can use it.
- **`camera_state` event is dedup'd.** The worker only pushes `camera_state` on real online↔offline transitions (see `_notify_camera_state`). Don't add a second push site without going through that helper, or USB flicker will spam the chat.
- **ffmpeg re-encodes to H.264 always** before send — `mp4v` fourcc won't play inline on Telegram. The codec choice in `camera.py` is independent; bot-side re-encode is required for delivery.
- **No auto-deletion** of recordings. Operator decides when to clear `recordings/`. Disk-space guard *blocks new recordings* below 1 GB free but never deletes.

## Configuration

- `config.ini` — checked-in defaults; safe to edit. Sections: `[camera]`, `[detection]`, `[recording]`, `[telegram]`.
- `.env` — **never committed**, contains `BOT_TOKEN` and `CHAT_ID`. `.env.example` has placeholders only.
- Numeric `CHAT_ID` (Telegram user/chat ID), not a username. Obtain via `@userinfobot`.

## Working in this repo

- Do not commit `.env`, anything under `recordings/`, `logs/`, `captures/`, or `venv/`. All gitignored — keep it that way.
- Do not weaken the path-traversal guard in `bot.py` or the current-file guard in the recordings handler. Both have dedicated verification steps in `TESTING.md` (28, 27).
- Don't add unit tests that mock OpenCV / Telegram — the project's testing model is hands-on, documented in `TESTING.md`. Real regressions come from real hardware.
- If you change the event schema between `camera.py` and `bot.py`, update both ends in the same commit and re-verify against the contract documented in the archived plan (`archive/2026-05-11_*.md`, section "Event pipeline contract").

## Operator docs

- `SETUP.md` — one-time Windows manual config (USB selective suspend, monitor sleep, screensaver, camera driver). Required before first run.
- `TESTING.md` — verification procedures (camera enumeration, codec smoke test, ROI, motion calibration, lock-screen ops, sleep prevention, USB suspend, large-file re-encode, current-file guard, path-traversal guard, disk-space warning).
- `archive/2026-05-11_*.md` — the original implementation plan, frozen as historical reference. Don't edit; supersede via newer commits if behavior changes.

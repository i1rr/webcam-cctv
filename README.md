# webTelegramCCTV

Windows desktop CCTV with Logitech Brio camera + Telegram bot motion alerts.

The application watches a configurable region of the camera frame for motion, records segmented MP4 clips while motion persists, and notifies a single authorized Telegram chat with a snapshot when recording starts and a summary when it ends. Past recordings are browsable and re-sendable through the bot.

## Quick Start

1. Install **Python 3.13**.
2. Create a virtual environment in the project root and activate it:

   ```powershell
   python -m venv venv
   venv\Scripts\activate
   ```
3. Install Python dependencies:

   ```powershell
   pip install -r requirements.txt
   ```
4. Install **ffmpeg**. Either add `ffmpeg.exe` to your `PATH` or drop `ffmpeg.exe` next to `main.py` in the project directory. Required for H.264 re-encoding of large recordings before sending to Telegram.
5. Copy `.env.example` to `.env` and fill in your bot credentials:

   ```
   BOT_TOKEN=<token from @BotFather>
   CHAT_ID=<numeric Telegram chat ID of the authorized user>
   ```
6. Run the interactive ROI selector once to mark the motion zone (e.g., a door area):

   ```powershell
   python setup_roi.py
   ```

   Drag a rectangle, press ENTER. The normalized coordinates are written to `config.ini` under `[detection]`.
7. Complete the **one-time Windows setup** described in [SETUP.md](SETUP.md) (USB selective suspend, screen saver, monitor sleep, Brio driver check). System sleep is suppressed programmatically and needs no manual setting.
8. Start the monitor:

   ```
   start_cctv.bat
   ```

   Send `/start` to the bot to confirm it is reachable. Use `/stop` to shut down remotely (useful when the workstation is locked).

After installation, work through [TESTING.md](TESTING.md) to verify camera enumeration, motion calibration, locked-session operation, large-file delivery, and the security guards before relying on the system.

## Module Map

- `main.py` — orchestration and lifecycle (sleep prevention, Brio LED, graceful shutdown), wires camera events to the bot.
- `camera.py` — `VideoCapture` loop, MOG2 motion detection inside the configured ROI, debounced state machine, segmented MP4 recording, snapshot-on-alert, disk-space guard.
- `bot.py` — `python-telegram-bot` `Application`; `/start` and `/stop` commands plus inline-keyboard callbacks (status, recordings list, file send); ffmpeg re-encode for oversized files; current-recording and path-traversal guards.
- `config.py` — `Config` dataclass loading `config.ini` plus `BOT_TOKEN` / `CHAT_ID` from `.env`.
- `windows_utils.py` — `SetThreadExecutionState` wrapper to suppress system sleep; best-effort Logitech Brio HID LED control.
- `setup_roi.py` — interactive OpenCV ROI selector; writes normalized coordinates to `config.ini`.
- `start_cctv.bat` — Windows launcher: activates `venv` and runs `python main.py`.
- `config.ini` — tunables (camera index/resolution/fps, detection thresholds, ROI, segment length, Telegram size cap, snapshot flag).
- `SETUP.md` — manual Windows power-settings checklist (one-time).
- `TESTING.md` — operator-facing verification procedures.

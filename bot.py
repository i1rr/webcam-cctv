import asyncio, logging, os, re, subprocess, shutil
from pathlib import Path
from typing import Awaitable, Callable
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, NetworkError, TimedOut, RetryAfter
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes,
)
from config import Config
from camera import CameraWorker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 12: Video compression / re-encoding helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Step 13: Application, handlers, event consumer
# ---------------------------------------------------------------------------

# Strict pattern: only filenames produced by camera.py; \Z prevents trailing-newline bypass
_SAFE_FILENAME_RE = re.compile(r'\Arecording_\d{8}_\d{6}\.mp4\Z')

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📹 Recordings", callback_data="menu_recordings")],
    [InlineKeyboardButton("📊 Status", callback_data="menu_status")],
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
    await update.message.reply_text("🎥 CCTV Monitor active.\nChoose an action:", reply_markup=MAIN_MENU)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remote stop via Telegram — useful when desktop is locked and Ctrl+C is unavailable."""
    cfg: Config = context.bot_data["config"]
    if not _authorized(update, cfg):
        return
    await update.message.reply_text("🛑 CCTV Monitor shutting down…")
    context.bot_data["stop_event"].set()


async def _safe_edit(query, text: str, **kwargs):
    """edit_message_text wrapper that swallows the 'Message is not modified'
    BadRequest Telegram raises when the user re-taps a button whose result
    is identical to the message's current state. Real failures still bubble."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


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
        label = "🔴 Recording" if state == "RECORDING" else "🟢 Idle"
        await _safe_edit(query, f"State: {label}", reply_markup=MAIN_MENU)

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
            await _safe_edit(query, "No recordings.", reply_markup=MAIN_MENU)
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
                f"📄 {f.name}{suffix} ({size_mb} MB)",
                callback_data=f"send_{f.name}",
            )])
        buttons.append([InlineKeyboardButton("← Back", callback_data="menu_back")])
        await _safe_edit(
            query,
            "Choose a recording to send (🔴 = currently recording, unavailable):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif query.data.startswith("send_"):
        filename = query.data[5:]
        file_path = _safe_file_path(filename, cfg.output_dir)
        if not file_path or not os.path.exists(file_path):
            await _safe_edit(query, "File not found or invalid name.", reply_markup=MAIN_MENU)
            return

        # Refuse to send the currently-recording file (would produce a corrupt/truncated video)
        active = worker.get_current_file()
        if active and os.path.abspath(active) == os.path.abspath(file_path):
            await _safe_edit(
                query,
                "⏳ This recording is in progress — wait for it to finish.",
                reply_markup=MAIN_MENU,
            )
            return

        await _safe_edit(query, f"⏳ Preparing and sending {filename}…")
        try:
            send_path = await asyncio.to_thread(compress_for_telegram, file_path, cfg.max_send_size_mb)
        except Exception as e:
            log.exception("compress_for_telegram failed for %s", filename)
            await context.bot.send_message(
                chat_id=cfg.chat_id,
                text=f"⚠️ Error preparing file: {e}",
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
                text=f"⚠️ Error sending video: {e}",
                reply_markup=MAIN_MENU,
            )
        else:
            await context.bot.send_message(
                chat_id=cfg.chat_id,
                text="✅ Done.",
                reply_markup=MAIN_MENU,
            )
        finally:
            if send_path != file_path and os.path.exists(send_path):
                os.remove(send_path)

    elif query.data == "menu_back":
        await _safe_edit(query, "Choose an action:", reply_markup=MAIN_MENU)


async def _send_with_retry(attempt: Callable[[], Awaitable], *, what: str):
    """Retry a Telegram send forever on transient network errors.

    `attempt` must be a 0-arg async callable that returns a fresh coroutine each
    call — awaited coroutines aren't re-awaitable, and senders that open files
    need to re-open them per attempt. Backoff: 5s → 10s → 20s → 40s → 60s (cap).
    Cancels cleanly when the consumer task is cancelled at shutdown."""
    delay = 5.0
    n = 0
    while True:
        n += 1
        try:
            return await attempt()
        except RetryAfter as e:
            wait = float(e.retry_after) + 1.0
            log.warning("Telegram rate-limited (%s): waiting %.0fs", what, wait)
            await asyncio.sleep(wait)
        except (NetworkError, TimedOut) as e:
            log.warning("Telegram %s failed (attempt %d): %s — retrying in %.0fs",
                        what, n, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def process_camera_events(app: Application, queue: asyncio.Queue, cfg: Config):
    """Drains camera event queue and dispatches Telegram messages. Runs forever.

    Each send is retried on NetworkError/TimedOut so a Wi-Fi blackout doesn't
    drop motion alerts — events queue up locally and flush in order once the
    network returns."""
    while True:
        event = await queue.get()
        try:
            if event["type"] == "motion_start":
                snap = event.get("snapshot_path")
                if snap and cfg.snapshot_on_alert and os.path.exists(snap):
                    async def _send_photo():
                        # Re-open per attempt: InputFile consumes the handle
                        with open(snap, "rb") as f:
                            return await app.bot.send_photo(
                                chat_id=cfg.chat_id,
                                photo=InputFile(f),
                                caption="🚨 Motion detected! Recording started.",
                            )
                    try:
                        await _send_with_retry(_send_photo, what="motion_start photo")
                    except FileNotFoundError:
                        # Snapshot got cleaned up between queueing and sending — text fallback
                        await _send_with_retry(
                            lambda: app.bot.send_message(
                                chat_id=cfg.chat_id,
                                text="🚨 Motion detected! Recording started.",
                            ),
                            what="motion_start message (fallback)",
                        )
                else:
                    await _send_with_retry(
                        lambda: app.bot.send_message(
                            chat_id=cfg.chat_id,
                            text="🚨 Motion detected! Recording started.",
                        ),
                        what="motion_start message",
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
                    await _send_with_retry(
                        lambda: app.bot.send_message(
                            chat_id=cfg.chat_id,
                            text=(f"✅ Recording finished: {fname} ({size_mb:.1f} MB)\n"
                                  "Use the 📹 menu to view."),
                            reply_markup=MAIN_MENU,
                        ),
                        what="recording_saved message",
                    )

            elif event["type"] == "camera_error":
                await _send_with_retry(
                    lambda: app.bot.send_message(
                        chat_id=cfg.chat_id,
                        text=f"⚠️ Camera error: {event.get('message', 'Unknown')}",
                    ),
                    what="camera_error message",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error processing camera event %s", event.get("type"))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Top-level handler so PTB stops logging 'No error handlers are registered'.
    Long-poll network blips are auto-retried by Updater; we just downgrade their
    log level so cctv.log isn't swamped by overnight Wi-Fi stutters."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.info("Transient network error in handler: %s", err)
    else:
        log.error("Unhandled error in bot handler", exc_info=err)


def build_application(cfg: Config, camera_worker: CameraWorker) -> Application:
    # PTB defaults are 5s for every phase, which a single Wi-Fi blip can blow
    # through. Bumping connect/read/write to 30s and pool to 5s means a missed
    # alert needs >30s of real network outage, not a momentary stutter.
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(5.0)
        .build()
    )
    app.bot_data["config"] = cfg
    app.bot_data["camera_worker"] = camera_worker
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(cb_menu))
    app.add_error_handler(on_error)
    return app

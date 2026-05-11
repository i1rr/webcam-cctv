import asyncio, datetime, logging, os, re, subprocess, shutil
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
# Video compression / re-encoding helper
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
# Application, handlers, event consumer
# ---------------------------------------------------------------------------

# Strict pattern: only filenames produced by camera.py; \Z prevents trailing-newline bypass
_SAFE_FILENAME_RE = re.compile(r'\Arecording_\d{8}_\d{6}\.mp4\Z')
IMPORTANT_SUBDIR = "important"
# Telegram bot upload cap is 50 MB. We compare against 49.0 to leave a margin
# for HTTP overhead; anything larger is announced via text instead of upload.
TELEGRAM_BOT_UPLOAD_LIMIT_MB = 49.0

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📹 Recordings", callback_data="menu_recordings")],
    [InlineKeyboardButton("📊 Status", callback_data="menu_status")],
    [InlineKeyboardButton("🗑️ Cleanup", callback_data="menu_cleanup")],
])


def _authorized(update: Update, cfg: Config) -> bool:
    return update.effective_chat.id == cfg.chat_id


def _pretty_name(filename: str) -> str:
    """recording_20260512_073207.mp4 → 'May 12, 07:32:07'. Falls back to the
    raw filename if the pattern doesn't match or the date is invalid."""
    m = re.match(r'recording_(\d{8})_(\d{6})\.mp4', filename)
    if not m:
        return filename
    try:
        dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.strftime("%b %d, %H:%M:%S")
    except ValueError:
        return filename


def _resolve_recording(filename: str, cfg: Config) -> tuple[str | None, bool]:
    """Find a recording by basename in regular or important folder.
    Returns (absolute_path, is_important). Path-traversal-safe via
    _SAFE_FILENAME_RE which forbids '/', '\\', '..'."""
    if not _SAFE_FILENAME_RE.match(filename):
        return None, False
    base = Path(cfg.output_dir)
    regular = base / filename
    if regular.exists():
        return str(regular.resolve()), False
    important = base / IMPORTANT_SUBDIR / filename
    if important.exists():
        return str(important.resolve()), True
    return None, False


def _list_recordings(cfg: Config) -> list[tuple[Path, bool]]:
    """Return [(path, is_important)] sorted by mtime descending. Filters by
    _SAFE_FILENAME_RE so transient _tg.mp4 encode artifacts are excluded."""
    base = Path(cfg.output_dir)
    important_dir = base / IMPORTANT_SUBDIR
    items: list[tuple[Path, bool]] = []
    for folder, is_imp in ((base, False), (important_dir, True)):
        try:
            for p in folder.glob("recording_*.mp4"):
                if p.is_file() and _SAFE_FILENAME_RE.match(p.name):
                    items.append((p, is_imp))
        except OSError:
            pass
    try:
        items.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    except OSError:
        pass
    return items


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


def _back_button(target: str = "menu_back", label: str = "← Back") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=target)


# ---------------------------------------------------------------------------
# Menu handlers (one per callback_data prefix)
# ---------------------------------------------------------------------------

async def _show_recordings_list(query, cfg: Config, worker: CameraWorker):
    items = _list_recordings(cfg)[:15]
    if not items:
        await _safe_edit(query, "No recordings.", reply_markup=MAIN_MENU)
        return
    active_file = worker.get_current_file()
    active_name = os.path.basename(active_file) if active_file else None
    buttons = []
    for path, is_important in items:
        try:
            size_mb = path.stat().st_size // (1024 * 1024)
        except OSError:
            size_mb = 0
        icon = "⭐" if is_important else "📄"
        suffix = " 🔴" if path.name == active_name else ""
        pretty = _pretty_name(path.name)
        buttons.append([InlineKeyboardButton(
            f"{icon} {pretty}{suffix} ({size_mb} MB)",
            callback_data=f"act_{path.name}",
        )])
    buttons.append([_back_button()])
    await _safe_edit(
        query,
        "Tap a recording to act on it (🔴 = currently recording):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_file_actions(query, filename: str, cfg: Config, worker: CameraWorker):
    abs_path, is_important = _resolve_recording(filename, cfg)
    if not abs_path:
        await _safe_edit(query, "File not found.", reply_markup=MAIN_MENU)
        return
    active = worker.get_current_file()
    is_active = active and os.path.abspath(active) == abs_path
    icon = "⭐" if is_important else "📄"
    header = f"{icon} {_pretty_name(filename)}"
    if is_active:
        await _safe_edit(
            query,
            f"{header}\n\n⏳ Currently recording — actions unavailable.",
            reply_markup=InlineKeyboardMarkup([[_back_button("menu_recordings", "← Back to recordings")]]),
        )
        return
    mark_label = "💼 Remove from important" if is_important else "⭐ Mark important"
    buttons = [
        [InlineKeyboardButton("📤 Send", callback_data=f"send_{filename}")],
        [InlineKeyboardButton(mark_label, callback_data=f"mark_{filename}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{filename}")],
        [_back_button("menu_recordings", "← Back to recordings")],
    ]
    await _safe_edit(query, header, reply_markup=InlineKeyboardMarkup(buttons))


async def _send_recording(context, query, filename: str, cfg: Config, worker: CameraWorker):
    abs_path, _is_imp = _resolve_recording(filename, cfg)
    if not abs_path:
        await _safe_edit(query, "File not found.", reply_markup=MAIN_MENU)
        return
    active = worker.get_current_file()
    if active and os.path.abspath(active) == abs_path:
        await _safe_edit(query, "⏳ Recording in progress — wait for it to finish.", reply_markup=MAIN_MENU)
        return
    pretty = _pretty_name(filename)
    await _safe_edit(query, f"⏳ Preparing and sending {pretty}…")
    try:
        send_path = await asyncio.to_thread(compress_for_telegram, abs_path, cfg.max_send_size_mb)
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
                caption=pretty,
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
            text="✅ Sent.",
            reply_markup=MAIN_MENU,
        )
    finally:
        if send_path != abs_path and os.path.exists(send_path):
            try:
                os.remove(send_path)
            except OSError:
                pass


async def _toggle_important(query, filename: str, cfg: Config, worker: CameraWorker):
    abs_path, is_important = _resolve_recording(filename, cfg)
    if not abs_path:
        await _safe_edit(query, "File not found.", reply_markup=MAIN_MENU)
        return
    active = worker.get_current_file()
    if active and os.path.abspath(active) == abs_path:
        await _safe_edit(query, "⏳ Recording in progress — cannot move now.", reply_markup=MAIN_MENU)
        return
    base = Path(cfg.output_dir)
    important_dir = base / IMPORTANT_SUBDIR
    try:
        if is_important:
            dest = base / filename
            Path(abs_path).rename(dest)
            text = f"💼 Removed from important:\n{_pretty_name(filename)}"
        else:
            important_dir.mkdir(parents=True, exist_ok=True)
            dest = important_dir / filename
            Path(abs_path).rename(dest)
            text = f"⭐ Marked important:\n{_pretty_name(filename)}"
    except OSError as e:
        log.exception("toggle important failed for %s", filename)
        text = f"⚠️ Operation failed: {e}"
    await _safe_edit(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([[_back_button("menu_recordings", "← Back to recordings")]]),
    )


async def _confirm_delete(query, filename: str, cfg: Config):
    abs_path, is_important = _resolve_recording(filename, cfg)
    if not abs_path:
        await _safe_edit(query, "File not found.", reply_markup=MAIN_MENU)
        return
    icon = "⭐" if is_important else "📄"
    text = (f"Delete this recording?\n\n{icon} {_pretty_name(filename)}\n\n"
            "This cannot be undone.")
    buttons = [
        [InlineKeyboardButton("🗑️ Yes, delete", callback_data=f"delok_{filename}")],
        [_back_button("menu_recordings", "← Cancel")],
    ]
    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(buttons))


async def _execute_delete(query, filename: str, cfg: Config, worker: CameraWorker):
    abs_path, _is_imp = _resolve_recording(filename, cfg)
    if not abs_path:
        await _safe_edit(query, "File not found.", reply_markup=MAIN_MENU)
        return
    active = worker.get_current_file()
    if active and os.path.abspath(active) == abs_path:
        await _safe_edit(query, "⏳ Recording in progress — cannot delete.", reply_markup=MAIN_MENU)
        return
    try:
        os.remove(abs_path)
        text = f"🗑️ Deleted:\n{_pretty_name(filename)}"
    except OSError as e:
        log.exception("delete failed for %s", filename)
        text = f"⚠️ Delete failed: {e}"
    await _safe_edit(
        query,
        text,
        reply_markup=InlineKeyboardMarkup([[_back_button("menu_recordings", "← Back to recordings")]]),
    )


def _count_cleanup_candidates(cfg: Config, worker: CameraWorker) -> int:
    """Count non-important recordings that bulk-cleanup would delete.
    Excludes the currently-recording file so cleanup never races the writer."""
    active = worker.get_current_file()
    active_resolved = str(Path(active).resolve()) if active else None
    count = 0
    for path, is_important in _list_recordings(cfg):
        if is_important:
            continue
        if active_resolved and str(path.resolve()) == active_resolved:
            continue
        count += 1
    return count


async def _cleanup_step1(query, cfg: Config, worker: CameraWorker):
    n = _count_cleanup_candidates(cfg, worker)
    if n == 0:
        await _safe_edit(query, "Nothing to clean up — no non-important recordings.", reply_markup=MAIN_MENU)
        return
    text = (f"⚠️ Cleanup will delete {n} non-important recording(s).\n\n"
            "⭐ Important recordings will be kept.")
    buttons = [
        [InlineKeyboardButton(f"🗑️ Delete {n} recordings", callback_data="cleanall_ask2")],
        [_back_button()],
    ]
    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(buttons))


async def _cleanup_step2(query, cfg: Config, worker: CameraWorker):
    n = _count_cleanup_candidates(cfg, worker)
    if n == 0:
        await _safe_edit(query, "Nothing to clean up.", reply_markup=MAIN_MENU)
        return
    text = f"⚠️ Really delete {n} recording(s)?\n\nThis cannot be undone."
    buttons = [
        [InlineKeyboardButton("✅ Yes, really delete", callback_data="cleanall_go")],
        [_back_button()],
    ]
    await _safe_edit(query, text, reply_markup=InlineKeyboardMarkup(buttons))


async def _cleanup_execute(query, cfg: Config, worker: CameraWorker):
    base = Path(cfg.output_dir)
    active = worker.get_current_file()
    active_resolved = str(Path(active).resolve()) if active else None
    deleted = 0
    failed = 0
    snaps_deleted = 0
    try:
        for p in base.iterdir():
            if not p.is_file():
                continue  # the important/ subdir is a directory; left alone
            if active_resolved and str(p.resolve()) == active_resolved:
                continue
            if p.name.startswith("recording_") and p.suffix == ".mp4":
                try:
                    p.unlink()
                    deleted += 1
                except OSError as e:
                    log.warning("Failed to delete %s: %s", p, e)
                    failed += 1
            elif p.name.startswith("snap_") and p.suffix == ".jpg":
                try:
                    p.unlink()
                    snaps_deleted += 1
                except OSError:
                    pass  # snapshots are best-effort
    except OSError as e:
        log.exception("cleanup iteration failed")
        await _safe_edit(query, f"⚠️ Cleanup error: {e}", reply_markup=MAIN_MENU)
        return
    text = f"🗑️ Deleted {deleted} recording(s)"
    if snaps_deleted:
        text += f" and {snaps_deleted} snapshot(s)"
    text += "."
    if failed:
        text += f"\n({failed} files could not be deleted — see log)"
    await _safe_edit(query, text, reply_markup=MAIN_MENU)


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cfg: Config = context.bot_data["config"]
    worker: CameraWorker = context.bot_data["camera_worker"]

    if not _authorized(update, cfg):
        await query.answer()
        return

    await query.answer()
    data = query.data

    if data == "menu_status":
        state = worker.get_state()
        label = "🔴 Recording" if state == "RECORDING" else "🟢 Idle"
        await _safe_edit(query, f"State: {label}", reply_markup=MAIN_MENU)
    elif data == "menu_recordings":
        await _show_recordings_list(query, cfg, worker)
    elif data == "menu_cleanup":
        await _cleanup_step1(query, cfg, worker)
    elif data == "cleanall_ask2":
        await _cleanup_step2(query, cfg, worker)
    elif data == "cleanall_go":
        await _cleanup_execute(query, cfg, worker)
    elif data == "menu_back":
        await _safe_edit(query, "Choose an action:", reply_markup=MAIN_MENU)
    elif data.startswith("act_"):
        await _show_file_actions(query, data[4:], cfg, worker)
    elif data.startswith("send_"):
        await _send_recording(context, query, data[5:], cfg, worker)
    elif data.startswith("mark_"):
        await _toggle_important(query, data[5:], cfg, worker)
    elif data.startswith("delok_"):
        await _execute_delete(query, data[6:], cfg, worker)
    elif data.startswith("del_"):
        await _confirm_delete(query, data[4:], cfg)


# ---------------------------------------------------------------------------
# Camera event consumer + outbound retry
# ---------------------------------------------------------------------------

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


async def _handle_recording_saved(app: Application, event: dict, cfg: Config):
    """Auto-send a just-finished recording (every segment, including rotations).
    Compresses to H.264 first, then send_video with retry. Falls back to a
    text notice if the compressed file exceeds Telegram's bot upload limit."""
    fpath = event.get("file_path", "")
    is_rotation = event.get("is_segment_rotation", False)
    if not fpath or not os.path.exists(fpath):
        log.warning("recording_saved without valid file: %r", fpath)
        return
    fname = os.path.basename(fpath)
    pretty = _pretty_name(fname)

    try:
        send_path = await asyncio.to_thread(compress_for_telegram, fpath, cfg.max_send_size_mb)
    except Exception:
        log.exception("compress failed for %s; sending original", fpath)
        send_path = fpath

    try:
        size_mb = (os.path.getsize(send_path) / (1024 * 1024)
                   if os.path.exists(send_path) else 0)
        icon = "📼" if is_rotation else "✅"

        if size_mb > TELEGRAM_BOT_UPLOAD_LIMIT_MB:
            log.warning("Auto-send skipped: %s is %.1f MB > %.1f MB",
                        fname, size_mb, TELEGRAM_BOT_UPLOAD_LIMIT_MB)
            await _send_with_retry(
                lambda: app.bot.send_message(
                    chat_id=cfg.chat_id,
                    text=(f"{icon} Recording too large to auto-send "
                          f"({size_mb:.1f} MB)\n{pretty}\n"
                          "Use the 📹 menu to retrieve it if needed."),
                    reply_markup=MAIN_MENU,
                ),
                what="recording_saved oversize-notice",
            )
            return

        async def _send_video():
            with open(send_path, "rb") as f:
                return await app.bot.send_video(
                    chat_id=cfg.chat_id,
                    video=InputFile(f, filename=fname),
                    supports_streaming=True,
                    caption=f"{icon} {pretty} ({size_mb:.1f} MB)",
                    reply_markup=MAIN_MENU,
                )
        try:
            await _send_with_retry(_send_video, what="recording_saved video")
        except FileNotFoundError:
            log.warning("Recording disappeared before send: %s", send_path)
    finally:
        if send_path != fpath and os.path.exists(send_path):
            try:
                os.remove(send_path)
            except OSError:
                pass


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
                # Auto-send every segment (rotations and final alike) so the
                # operator gets the full event chronologically.
                await _handle_recording_saved(app, event, cfg)

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

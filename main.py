import asyncio, threading, logging, atexit, os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from config import load_config
from camera import CameraWorker
from bot import build_application, process_camera_events
from windows_utils import prevent_sleep, allow_sleep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler("cctv.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
# httpx/httpcore INFO logs include the full Telegram URL — which contains the
# bot token. Drop them to WARNING so tokens never reach cctv.log or the console.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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

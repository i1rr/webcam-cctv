import cv2, threading, time, os, asyncio, logging, shutil
from datetime import datetime
from pathlib import Path
from config import Config

log = logging.getLogger(__name__)

def _try_open_writer(path: str, fps: float, size: tuple) -> cv2.VideoWriter:
    """Open an mp4v VideoWriter. Telegram delivery re-encodes to H.264 via ffmpeg,
    so native avc1 isn't worth the OpenH264 DLL dance."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, fps, size)
    if writer.isOpened():
        return writer
    writer.release()
    raise RuntimeError(f"Cannot open VideoWriter for {path}: mp4v codec unavailable")


class CameraWorker:
    def __init__(self, config: Config, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
        self.cfg = config
        self.loop = loop
        self.queue = event_queue
        self._stop = threading.Event()
        # Default ON so app starts with the original behaviour. Bot can clear/set
        # this at runtime to release/reacquire the USB device without restarting.
        self._enabled = threading.Event()
        self._enabled.set()
        self.state = "IDLE"
        self.last_motion_time = 0.0
        self.debounce_count = 0
        self.writer: cv2.VideoWriter | None = None
        self.current_file: str | None = None
        self.segment_start_time: float = 0.0
        # Latches when disk space drops below cfg.min_free_gb; cleared once it
        # recovers. Prevents one Telegram warning per frame while motion persists.
        self._low_disk_warned: bool = False
        # Latches when the capture device fails to open. Reset on user re-enable
        # so a manual toggle always gets a fresh diagnostic if it still fails.
        self._camera_error_warned: bool = False
        # Tracks the last camera_state event we pushed so flicker (open succeeds
        # then read fails repeatedly) doesn't spam the chat with state events.
        # None means "no state pushed yet" — the first push always fires.
        self._last_state_notified: bool | None = None

    def run(self):
        Path(self.cfg.output_dir).mkdir(parents=True, exist_ok=True)
        try:
            while not self._stop.is_set():
                # Sleep here when toggled off. Releases nothing because nothing
                # is held — the capture device is opened only inside the inner
                # session below, so a disabled worker leaves the camera free
                # for other apps and the Brio LED stays dark.
                if not self._enabled.is_set():
                    self._wait_for_enable()
                    if self._stop.is_set():
                        break
                self._run_capture_session()
        finally:
            # Defensive: a writer leak past run() leaves a 0-byte mp4 on disk.
            if self.state == "RECORDING":
                self._stop_recording()
            log.info("Camera worker stopped")

    def _notify_camera_state(self, online: bool):
        """Push a camera_state event only on actual transitions. Read failures
        that auto-recover within one retry don't reach the chat."""
        if self._last_state_notified is online:
            return
        self._last_state_notified = online
        self._push({"type": "camera_state", "enabled": online})

    def _wait_for_enable(self):
        """Block until enable() is called or shutdown. stop() always sets
        _enabled too, so a single wait() unblocks for either signal — the
        outer run() loop's _stop check then decides which path was taken."""
        log.info("Camera disabled — capture released, waiting for enable")
        self._notify_camera_state(False)
        self._enabled.wait()

    def _run_capture_session(self):
        """One open→loop→release cycle. Returns when stopped, disabled, or
        on unrecoverable open failure. Outer run() decides whether to retry."""
        cap = self._open_camera()
        if cap is None:
            if not self._camera_error_warned:
                self._push({"type": "camera_error",
                            "message": "Cannot open camera index " + str(self.cfg.camera_index)})
                self._camera_error_warned = True
            # Back off so a missing camera doesn't burn CPU. Bail early if the
            # user stops during the wait.
            self._stop.wait(timeout=5.0)
            return
        self._notify_camera_state(True)

        fgbg = cv2.createBackgroundSubtractorMOG2(
            history=self.cfg.mog2_history,
            varThreshold=self.cfg.mog2_var_threshold,
            detectShadows=False,
        )
        warmup_remaining = self.cfg.warmup_frames
        log.info("Capture session running (warmup: %d frames)", warmup_remaining)

        try:
            while not self._stop.is_set() and self._enabled.is_set():
                ret, frame = cap.read()
                if not ret:
                    log.warning("Frame read failed — closing session, will retry in 2s")
                    self._stop.wait(timeout=2.0)
                    return  # outer run() reopens

                # Apply MOG2 to full frame to maintain consistent background model
                full_mask = fgbg.apply(frame)

                if warmup_remaining > 0:
                    warmup_remaining -= 1
                    continue

                now = time.monotonic()

                if self.state == "IDLE":
                    # Trigger only on ROI motion — keeps fan blades, TV flicker, and
                    # outside light changes from starting unwanted recordings.
                    if self._detect_motion_from_mask(full_mask, frame.shape, roi_only=True):
                        self.debounce_count += 1
                        if self.debounce_count >= max(self.cfg.debounce_frames, 1):
                            self.debounce_count = 0
                            self._start_recording(frame, now)
                    else:
                        self.debounce_count = 0

                elif self.state == "RECORDING":
                    # Once recording is justified, accept motion anywhere in the
                    # frame so an intruder who steps out of the ROI (e.g., rummaging
                    # in a wardrobe) doesn't end the recording prematurely.
                    if self._detect_motion_from_mask(full_mask, frame.shape, roi_only=False):
                        self.last_motion_time = now
                    self.writer.write(frame)

                    if (now - self.segment_start_time) / 60 >= self.cfg.segment_max_minutes:
                        self._rotate_segment(now)

                    elif now - self.last_motion_time >= max(self.cfg.motion_timeout_sec, 1):
                        self._stop_recording()
        finally:
            # Always flush any active recording before releasing the device, so
            # the in-progress segment is finalized and auto-sent regardless of
            # whether we left via stop, disable, or a read failure.
            if self.state == "RECORDING":
                self._stop_recording()
            cap.release()

    def stop(self):
        self._stop.set()
        # Unblock _wait_for_enable so the thread can observe the stop flag.
        self._enabled.set()

    def enable(self):
        """Resume capture. No-op if already enabled. Clears the open-failure
        latch so a fresh error fires if the next open also fails — the user
        just asked for the camera and deserves to know. Reset here (not only
        in _wait_for_enable) so a rapid disable→enable double-tap that the
        worker hasn't reacted to yet still gets the reset."""
        self._camera_error_warned = False
        self._enabled.set()

    def disable(self):
        """Pause capture and release the USB device. No-op if already disabled.
        Any in-progress recording is finalized by _run_capture_session's finally."""
        self._enabled.clear()

    def is_enabled(self) -> bool:
        return self._enabled.is_set()

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

    def _detect_motion_from_mask(self, full_mask, frame_shape, *, roi_only: bool) -> bool:
        """Find motion by contour area in the MOG2 mask.

        roi_only=True  — check activation zones (any hit triggers IDLE→RECORDING).
                         Keeps incidental motion (curtains, fan, light changes
                         outside watched areas) from starting recordings.
        roi_only=False — check sustain zones (any hit keeps a recording alive).

        Ignore zones (light leak behind the door, auto-brightness flicker spots)
        are zeroed in the mask before any zone is inspected, so they never
        count toward motion in any state.
        """
        h, w = frame_shape[:2]

        if self.cfg.ignore_zones:
            full_mask = full_mask.copy()  # don't mutate the caller's mask
            for zx1, zy1, zx2, zy2 in self.cfg.ignore_zones:
                ix1, iy1 = int(zx1 * w), int(zy1 * h)
                ix2, iy2 = int(zx2 * w), int(zy2 * h)
                if ix1 < ix2 and iy1 < iy2:
                    full_mask[iy1:iy2, ix1:ix2] = 0

        zones = self.cfg.activation_zones if roi_only else self.cfg.sustain_zones
        if not zones:
            log.warning("No %s zones configured — falling back to full frame",
                        "activation" if roi_only else "sustain")
            zones = [(0.0, 0.0, 1.0, 1.0)]

        threshold = self.cfg.min_contour_area
        for zx1, zy1, zx2, zy2 in zones:
            x1 = int(zx1 * w)
            y1 = int(zy1 * h)
            x2 = int(zx2 * w)
            y2 = int(zy2 * h)
            if x1 >= x2 or y1 >= y2:
                continue
            sub = full_mask[y1:y2, x1:x2]
            contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if any(cv2.contourArea(c) > threshold for c in contours):
                return True
        return False

    def _check_disk_space(self) -> bool:
        """Return True if free space >= cfg.min_free_gb. Pushes a warning event
        only on the OK→low transition so motion-triggered re-checks don't spam
        Telegram once per frame."""
        try:
            free_gb = shutil.disk_usage(self.cfg.output_dir).free / (1024 ** 3)
        except OSError:
            return True
        if free_gb < self.cfg.min_free_gb:
            if not self._low_disk_warned:
                msg = (f"Low disk space: {free_gb:.1f} GB free "
                       f"(threshold {self.cfg.min_free_gb:.1f} GB) — recording disabled")
                log.warning(msg)
                self._push({"type": "camera_error", "message": msg})
                self._low_disk_warned = True
            return False
        if self._low_disk_warned:
            log.info("Disk space recovered: %.1f GB free", free_gb)
            self._low_disk_warned = False
        return True

    def _start_recording(self, frame, now: float):
        if self._stop.is_set():
            return  # Shutdown in flight — don't start a recording we'll immediately abandon
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
        # Guard against double-call (e.g., motion-timeout branch finalizes the
        # segment on the same iteration the disable flag is observed, and the
        # session's finally block would otherwise push a second spurious
        # recording_saved with file_path=None).
        if self.state != "RECORDING":
            return
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
        # run_coroutine_threadsafe raises RuntimeError if the loop is closed,
        # which can happen if the camera thread is still draining its finally
        # block after main.py's join() timed out. Swallowing keeps shutdown
        # quiet; the event is lost but the bot is already gone.
        try:
            asyncio.run_coroutine_threadsafe(self.queue.put(event), self.loop)
        except RuntimeError:
            pass

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

    def _detect_motion_from_mask(self, full_mask, frame_shape, *, roi_only: bool) -> bool:
        """Find motion by contour area in the MOG2 mask.

        roi_only=True  — restrict the check to the configured door ROI. Used
                         from IDLE to start a recording. Keeps incidental
                         motion (curtains, fan, light changes) out of triggers.
        roi_only=False — check the whole frame. Used while already RECORDING
                         so an intruder who leaves the ROI keeps the recording
                         alive. False positives here are harmless because the
                         recording is already justified by the ROI trigger.
        """
        mask = full_mask
        if roi_only:
            h, w = frame_shape[:2]
            x1 = int(self.cfg.roi[0] * w)
            y1 = int(self.cfg.roi[1] * h)
            x2 = int(self.cfg.roi[2] * w)
            y2 = int(self.cfg.roi[3] * h)
            if x1 < x2 and y1 < y2:
                mask = full_mask[y1:y2, x1:x2]
            else:
                log.warning("ROI is invalid — falling back to full frame")
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
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

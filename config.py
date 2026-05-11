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

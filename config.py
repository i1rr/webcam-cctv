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
    activation_zones: list      # list of (x1,y1,x2,y2) — any hit triggers IDLE→RECORDING
    sustain_zones: list         # list of (x1,y1,x2,y2) — any hit sustains a recording
    ignore_zones: list          # list of (x1,y1,x2,y2) — zeroed before activation/sustain checks
    output_dir: str
    segment_max_minutes: int
    min_free_gb: float
    max_send_size_mb: int
    snapshot_on_alert: bool

    def __repr__(self) -> str:
        return (f"Config(chat_id={self.chat_id}, camera_index={self.camera_index}, "
                f"activation={len(self.activation_zones)}, sustain={len(self.sustain_zones)}, "
                f"ignore={len(self.ignore_zones)}, "
                f"output_dir={self.output_dir!r}, bot_token=***)")


def _load_zone_list(ini: configparser.ConfigParser, key: str, *, legacy_prefix: str) -> list:
    """Read `key` as a semicolon-separated zone list. If empty/absent, fall back
    to a single legacy rect at `<legacy_prefix>_x1..y2`. If neither is set,
    return [(0,0,1,1)] (whole frame) so detection still works."""
    zones = _parse_zones(ini.get("detection", key, fallback=""))
    if zones:
        return zones
    try:
        rect = (
            ini.getfloat("detection", f"{legacy_prefix}_x1"),
            ini.getfloat("detection", f"{legacy_prefix}_y1"),
            ini.getfloat("detection", f"{legacy_prefix}_x2"),
            ini.getfloat("detection", f"{legacy_prefix}_y2"),
        )
        if rect[0] < rect[2] and rect[1] < rect[3]:
            return [rect]
    except (configparser.NoOptionError, ValueError):
        pass
    return [(0.0, 0.0, 1.0, 1.0)]


def _parse_zones(raw: str) -> list:
    """Parse 'x1,y1,x2,y2 ; x1,y1,x2,y2' into a list of normalized tuples.
    Silently drops malformed or zero-area entries."""
    zones = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(p) for p in parts)
        except ValueError:
            continue
        if x1 >= x2 or y1 >= y2:
            continue
        zones.append((x1, y1, x2, y2))
    return zones

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
        activation_zones=_load_zone_list(ini, "activation_zones", legacy_prefix="roi"),
        sustain_zones=_load_zone_list(ini, "sustain_zones", legacy_prefix="sustain"),
        ignore_zones=_parse_zones(ini.get("detection", "ignore_zones", fallback="")),
        output_dir=ini.get("recording", "output_dir", fallback="recordings"),
        segment_max_minutes=ini.getint("recording", "segment_max_minutes", fallback=10),
        min_free_gb=ini.getfloat("recording", "min_free_gb", fallback=1.0),
        max_send_size_mb=ini.getint("telegram", "max_send_size_mb", fallback=45),
        snapshot_on_alert=ini.getboolean("telegram", "snapshot_on_alert", fallback=True),
    )

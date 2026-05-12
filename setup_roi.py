"""
Run: python setup_roi.py

Three zone types, switched with number keys. Each is a list — add as many
rectangles as you like.

  1  Activation zones  (green)  — any hit starts a recording from IDLE
  2  Sustain zones     (cyan)   — any hit keeps a recording alive
  3  Ignore zones      (red)    — zeroed before activation/sustain checks
                                    (light leaks, flicker, auto-brightness spots)

Mouse: click-and-drag to draw a rectangle.
Keys:
  1 / 2 / 3   switch active zone type
  ENTER       commit the drawn rectangle into the active list
  D           delete the last zone in the active list
  S           save all zones to config.ini and exit
  ESC         exit WITHOUT saving
"""
import cv2, configparser

cfg = configparser.ConfigParser()
cfg.read("config.ini")
cam_index = cfg.getint("camera", "index", fallback=0)
cam_w = cfg.getint("camera", "width", fallback=1280)
cam_h = cfg.getint("camera", "height", fallback=720)

if not cfg.has_section("detection"):
    cfg.add_section("detection")


def _parse_zones(raw: str):
    out = []
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
        out.append((x1, y1, x2, y2))
    return out


def _load_list(key: str, legacy_prefix: str):
    """Read `key` (semicolon-separated). If empty, migrate the legacy single
    rect at `<legacy_prefix>_x1..y2` if present."""
    zones = _parse_zones(cfg.get("detection", key, fallback=""))
    if zones:
        return zones
    try:
        rect = (
            cfg.getfloat("detection", f"{legacy_prefix}_x1"),
            cfg.getfloat("detection", f"{legacy_prefix}_y1"),
            cfg.getfloat("detection", f"{legacy_prefix}_x2"),
            cfg.getfloat("detection", f"{legacy_prefix}_y2"),
        )
        if rect[0] < rect[2] and rect[1] < rect[3]:
            return [rect]
    except (configparser.NoOptionError, ValueError):
        pass
    return []


MODE_ACTIVATION, MODE_SUSTAIN, MODE_IGNORE = 1, 2, 3

zones = {
    MODE_ACTIVATION: _load_list("activation_zones", "roi"),
    MODE_SUSTAIN: _load_list("sustain_zones", "sustain"),
    MODE_IGNORE: _parse_zones(cfg.get("detection", "ignore_zones", fallback="")),
}

MODE_LABEL = {MODE_ACTIVATION: "ACTIVATION", MODE_SUSTAIN: "SUSTAIN", MODE_IGNORE: "IGNORE"}
MODE_COLOR = {MODE_ACTIVATION: (0, 255, 0), MODE_SUSTAIN: (255, 255, 0), MODE_IGNORE: (0, 0, 255)}

mode = MODE_ACTIVATION
drawing = False
drag = [0, 0, 0, 0]
has_drag = False


def mouse_callback(event, x, y, flags, param):
    global drawing, drag, has_drag
    if event == cv2.EVENT_LBUTTONDOWN:
        drawing, has_drag = True, False
        drag[0], drag[1], drag[2], drag[3] = x, y, x, y
    elif event == cv2.EVENT_MOUSEMOVE and drawing:
        drag[2], drag[3] = x, y
    elif event == cv2.EVENT_LBUTTONUP:
        drawing, has_drag = False, True
        drag[2], drag[3] = x, y


cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_w)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
WIN = "Zones — 1/2/3 mode, ENTER commit, D del-last, S save, ESC cancel"
cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
cv2.resizeWindow(WIN, cam_w, cam_h)
cv2.moveWindow(WIN, 100, 100)
cv2.setWindowProperty(WIN, cv2.WND_PROP_TOPMOST, 1)
cv2.setMouseCallback(WIN, mouse_callback)


def _draw_norm_rect(img, rect, color, label):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = rect
    p1 = (int(x1 * w), int(y1 * h))
    p2 = (int(x2 * w), int(y2 * h))
    cv2.rectangle(img, p1, p2, color, 2)
    cv2.putText(img, label, (p1[0] + 4, p1[1] + 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)


def _commit_pending():
    global has_drag
    if not has_drag:
        return False
    x1 = min(drag[0], drag[2]) / cam_w
    y1 = min(drag[1], drag[3]) / cam_h
    x2 = max(drag[0], drag[2]) / cam_w
    y2 = max(drag[1], drag[3]) / cam_h
    if x1 >= x2 or y1 >= y2:
        return False
    zones[mode].append((x1, y1, x2, y2))
    has_drag = False
    return True


def _fmt_zones(zs):
    return " ; ".join(f"{z[0]:.4f},{z[1]:.4f},{z[2]:.4f},{z[3]:.4f}" for z in zs)


def _save():
    cfg["detection"]["activation_zones"] = _fmt_zones(zones[MODE_ACTIVATION])
    cfg["detection"]["sustain_zones"] = _fmt_zones(zones[MODE_SUSTAIN])
    cfg["detection"]["ignore_zones"] = _fmt_zones(zones[MODE_IGNORE])
    # Strip legacy single-rect keys so config.ini has one source of truth
    for k in ("roi_x1", "roi_y1", "roi_x2", "roi_y2",
              "sustain_x1", "sustain_y1", "sustain_x2", "sustain_y2"):
        cfg.remove_option("detection", k)
    with open("config.ini", "w") as f:
        cfg.write(f)


while True:
    ret, frame = cap.read()
    if not ret:
        break
    display = frame.copy()

    for m in (MODE_ACTIVATION, MODE_SUSTAIN, MODE_IGNORE):
        for i, z in enumerate(zones[m]):
            _draw_norm_rect(display, z, MODE_COLOR[m], f"{MODE_LABEL[m]} {i + 1}")

    if has_drag or drawing:
        cv2.rectangle(display, (drag[0], drag[1]), (drag[2], drag[3]),
                      MODE_COLOR[mode], 2)

    hint = (f"[mode: {MODE_LABEL[mode]} ({len(zones[mode])})]  "
            f"1/2/3 switch  ENTER add  D del-last  S save  ESC cancel")
    cv2.putText(display, hint, (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, MODE_COLOR[mode], 2)

    cv2.imshow(WIN, display)
    key = cv2.waitKey(1) & 0xFF

    if key == ord("1"):
        mode = MODE_ACTIVATION
    elif key == ord("2"):
        mode = MODE_SUSTAIN
    elif key == ord("3"):
        mode = MODE_IGNORE
    elif key in (ord("d"), ord("D")):
        if zones[mode]:
            removed = zones[mode].pop()
            print(f"Removed {MODE_LABEL[mode]} #{len(zones[mode]) + 1}: {removed}")
        else:
            print(f"No {MODE_LABEL[mode]} zones to remove")
    elif key == 13:  # ENTER
        if _commit_pending():
            print(f"Added {MODE_LABEL[mode]} #{len(zones[mode])}: {zones[mode][-1]}")
        else:
            print("Nothing to commit — draw a rectangle first")
    elif key in (ord("s"), ord("S")):
        _save()
        print("Saved to config.ini")
        for m in (MODE_ACTIVATION, MODE_SUSTAIN, MODE_IGNORE):
            print(f"  {MODE_LABEL[m]:11s} ({len(zones[m])}): {zones[m]}")
        break
    elif key == 27:  # ESC
        print("Cancelled — config.ini not modified")
        break

cap.release()
cv2.destroyAllWindows()

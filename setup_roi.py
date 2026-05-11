"""
Run once: python setup_roi.py
Click and drag a rectangle over the door area. ENTER to save, ESC to cancel.
"""
import cv2, configparser

cfg = configparser.ConfigParser()
cfg.read("config.ini")
cam_index = cfg.getint("camera", "index", fallback=0)
cam_w = cfg.getint("camera", "width", fallback=1280)
cam_h = cfg.getint("camera", "height", fallback=720)

drawing = False
rect = [0, 0, 0, 0]
has_selection = False

def mouse_callback(event, x, y, flags, param):
    global drawing, rect, has_selection
    if event == cv2.EVENT_LBUTTONDOWN:
        drawing, has_selection = True, False
        rect[0], rect[1] = x, y
    elif event == cv2.EVENT_MOUSEMOVE and drawing:
        rect[2], rect[3] = x, y
    elif event == cv2.EVENT_LBUTTONUP:
        drawing, has_selection = False, True
        rect[2], rect[3] = x, y

cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_w)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
cv2.namedWindow("Draw door zone — ENTER to save, ESC to cancel")
cv2.setMouseCallback("Draw door zone — ENTER to save, ESC to cancel", mouse_callback)

while True:
    ret, frame = cap.read()
    if not ret:
        break
    display = frame.copy()
    if has_selection:
        cv2.rectangle(display, (rect[0], rect[1]), (rect[2], rect[3]), (0, 255, 0), 2)
        cv2.putText(display, "ENTER = save", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.imshow("Draw door zone — ENTER to save, ESC to cancel", display)
    key = cv2.waitKey(1)
    if key == 13 and has_selection:  # ENTER — use has_selection flag, not coordinate truthiness
        h, w = frame.shape[:2]
        x1 = min(rect[0], rect[2]) / w
        y1 = min(rect[1], rect[3]) / h
        x2 = max(rect[0], rect[2]) / w
        y2 = max(rect[1], rect[3]) / h
        if x1 >= x2 or y1 >= y2:
            print("ROI has zero width or height — draw a larger rectangle")
            continue
        cfg["detection"]["roi_x1"] = f"{x1:.4f}"
        cfg["detection"]["roi_y1"] = f"{y1:.4f}"
        cfg["detection"]["roi_x2"] = f"{x2:.4f}"
        cfg["detection"]["roi_y2"] = f"{y2:.4f}"
        with open("config.ini", "w") as f:
            cfg.write(f)
        print(f"ROI saved: ({x1:.4f}, {y1:.4f}) → ({x2:.4f}, {y2:.4f})")
        break
    elif key == 27:
        print("Cancelled — ROI not changed")
        break

cap.release()
cv2.destroyAllWindows()

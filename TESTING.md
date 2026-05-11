# Testing & Verification

This document is the operator's verification checklist after completing installation and the manual Windows setup in `SETUP.md`. Work through each step in order and confirm the expected results before relying on the system in production.

## Step 19 — Camera index enumeration

If camera index 0 is wrong (e.g., multiple cameras), find the Brio:

```bash
python -c "
import cv2
for i in range(5):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    if cap.isOpened():
        print(f'Camera {i}: {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}')
        cap.release()
"
```

Set the correct index in `config.ini [camera] index`.

## Step 20 — Camera and VideoWriter smoke test

```bash
python -c "
import cv2
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
print('opened:', cap.isOpened(), 'w:', cap.get(3), 'h:', cap.get(4), 'fps:', cap.get(5))
cap.release()
for codec in ('avc1', 'mp4v'):
    cc = cv2.VideoWriter_fourcc(*codec)
    w = cv2.VideoWriter('test_codec.mp4', cc, 30.0, (1280, 720))
    print(codec, '→ opened:', w.isOpened()); w.release()
import os; os.remove('test_codec.mp4')
"
```

At least one codec must show `opened: True`. If only `mp4v` works, ensure ffmpeg is present for H.264 re-encoding.

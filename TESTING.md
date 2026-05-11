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

## Step 21 — ROI setup and verification

Run `python setup_roi.py`. Draw a rectangle over the door. Press ENTER. Verify `config.ini` has non-default `roi_*` values.

## Step 22 — Motion detection calibration

Start via `start_cctv.bat`. Wait approximately 3 seconds (warmup). Wave a hand in the door zone. Verify:

- `cctv.log`: `Recording started`
- Telegram: alert photo received
- After 30 s with no motion: `cctv.log` shows `Recording saved`, and Telegram delivers a final notification (not spammed during recording)

Tune `min_contour_area` (smaller = more sensitive) and `mog2_var_threshold` (larger = less noise-sensitive) as needed.

## Step 23 — Locked Windows operation test

1. Start `start_cctv.bat`.
2. Lock the workstation with `Win+L`.
3. Trigger motion in the configured ROI.
4. Unlock the workstation and verify:
   - A Telegram alert was delivered while the session was locked.
   - A new recording file is present in `recordings/`.
   - `cctv.log` shows continuous timestamps spanning the locked period (no gaps that indicate the process paused).

## Step 24 — Sleep prevention verification

Start the system, lock the workstation, and wait at least 35 minutes. Return to the PC and verify:

- The PC has not entered sleep.
- The bot still responds to `/start`.

## Step 25 — USB selective suspend verification

Let the monitor turn off (do not lock or sleep). Trigger motion and verify the camera still responds (recording starts, Telegram alert is delivered). If the camera does not respond, recheck the USB selective suspend setting from Step 7 of `SETUP.md`.

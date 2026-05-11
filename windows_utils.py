"""Windows-specific helpers: sleep prevention and Logitech Brio LED control."""

import ctypes
import logging

import hid

log = logging.getLogger(__name__)

# --- Sleep prevention (Step 5) ---------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep():
    """Prevent system sleep. Does NOT prevent monitor from turning off."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def allow_sleep():
    """Restore default sleep policy (call on clean shutdown)."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# --- Logitech Brio LED control (Step 6) ------------------------------------

LOGITECH_VID = 0x046D
BRIO_PIDS = [0x085E, 0x0893, 0x08E5]  # Known Brio 4K PID variants


def try_disable_brio_led() -> bool:
    """
    Best-effort: attempt to disable Brio activity LED via vendor HID report.
    Most firmware builds do NOT allow disabling the privacy LED while camera is active.
    Failure is fully silent (log only). No functionality depends on result.
    """
    for pid in BRIO_PIDS:
        try:
            dev = hid.device()
            dev.open(LOGITECH_VID, pid)
            dev.write([0x00, 0x09, 0x09, 0x00])
            dev.close()
            log.info("Brio LED control: success")
            return True
        except Exception:
            continue
    log.info("Brio LED control: not available for this firmware/model")
    return False

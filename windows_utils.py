"""Windows-specific helpers: sleep prevention."""

import ctypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep():
    """Prevent system sleep. Does NOT prevent monitor from turning off."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def allow_sleep():
    """Restore default sleep policy (call on clean shutdown)."""
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

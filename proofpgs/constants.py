"""Shared constants for the ProofPGS decoder."""

import time


# PQ (ST 2084) constants
PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875

# PGS segment type codes
SEG_PDS = 0x14  # Palette Definition
SEG_ODS = 0x15  # Object Definition
SEG_PCS = 0x16  # Presentation Composition
SEG_WDS = 0x17  # Window Definition
SEG_END = 0x80  # End of Display Set

# Recognised file extensions
SUP_EXTENSIONS = {".sup"}
CONTAINER_EXTENSIONS = {".mkv", ".m2ts", ".ts", ".mp4", ".m4v", ".avi", ".wmv"}


def format_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Analysis budget
# ---------------------------------------------------------------------------

# Wallclock budget for the track-listing analysis phase (seconds).
# FFmpeg is killed if analysis takes longer than this.
LISTING_BUDGET_S = 10.0

# Per-track PGS packet cap passed to FFmpeg via -frames:s.
# ~25 display sets at ~5 segments per DS.  Increase for more analysis
# accuracy; decrease for a faster listing.  FFmpeg exits naturally once
# every output track hits this cap.
ANALYSIS_MAX_PACKETS = 125


class Budget:
    """Lightweight wallclock budget tracker using time.monotonic()."""

    def __init__(self, total_seconds: float):
        self._start = time.monotonic()
        self._total = total_seconds

    def remaining(self) -> float:
        """Seconds left in the budget (clamped >= 0)."""
        return max(0.0, self._total - (time.monotonic() - self._start))

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def elapsed(self) -> float:
        """Wallclock seconds since the budget was created."""
        return time.monotonic() - self._start

    def deadline(self) -> float:
        """Absolute monotonic timestamp when the budget expires."""
        return self._start + self._total

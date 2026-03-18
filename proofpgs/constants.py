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
CONTAINER_EXTENSIONS = {".mkv", ".mk3d", ".m2ts"}
MATROSKA_EXTENSIONS = {".mkv", ".mk3d"}


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

# Target display sets for analysis, also used as the per-track FFmpeg
# packet cap via -frames:s.  In MKV one packet = one DS, so this
# is passed directly.  In M2TS one packet = one PGS segment (~5
# per DS), so the caller multiplies by TS_SEGMENTS_PER_DS.
#
# More samples improve estimation accuracy but slow down the listing
# phase — especially for M2TS over network storage where extraction
# can exceed the LISTING_BUDGET_S wallclock limit.  Can be reduced
# (e.g. to 50) if analysis speed is more important than accuracy.
ANALYSIS_MAX_DS = 125

# Typical number of PGS segments per display set in M2TS streams.
# Used to scale ANALYSIS_MAX_DS for M2TS containers.
TS_SEGMENTS_PER_DS = 5

# Default number of content display sets to render when the user
# accepts the interactive "cached" default (no additional extraction).
DEFAULT_INTERACTIVE_COUNT = 10


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

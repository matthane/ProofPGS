"""Shared constants for the ProofPGS decoder."""

import re
import time


# PQ (ST 2084) constants
PQ_M1 = 0.1593017578125
PQ_M2 = 78.84375
PQ_C1 = 0.8359375
PQ_C2 = 18.8515625
PQ_C3 = 18.6875

# Recognised file extensions
SUP_EXTENSIONS = {".sup"}
CONTAINER_EXTENSIONS = {".mkv", ".mk3d", ".m2ts"}


def format_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# Matches HH:MM:SS.ms, MM:SS.ms, SS.ms, or plain seconds (e.g. 300, 5.5).
_TIMESTAMP_RE = re.compile(
    r'^(?:'
    r'(?P<h>\d+):(?P<m1>\d{1,2}):(?P<s1>\d{1,2}(?:\.\d+)?)'  # HH:MM:SS[.ms]
    r'|(?P<m2>\d{1,2}):(?P<s2>\d{1,2}(?:\.\d+)?)'             # MM:SS[.ms]
    r'|(?P<s3>\d+(?:\.\d+)?)'                                   # SS[.ms] or plain seconds
    r')$'
)


def parse_timestamp(ts: str) -> float:
    """Validate a timestamp string and return total seconds.

    Accepts the same formats as libpgs: ``HH:MM:SS.ms``, ``MM:SS.ms``,
    ``SS.ms``, or plain seconds (e.g. ``300``).

    Raises ``ValueError`` on invalid input.
    """
    m = _TIMESTAMP_RE.match(ts.strip())
    if not m:
        raise ValueError(f"Invalid timestamp: {ts!r} "
                         f"(expected HH:MM:SS.ms, MM:SS.ms, SS.ms, or seconds)")
    if m.group("h") is not None:
        return int(m.group("h")) * 3600 + int(m.group("m1")) * 60 + float(m.group("s1"))
    if m.group("m2") is not None:
        return int(m.group("m2")) * 60 + float(m.group("s2"))
    return float(m.group("s3"))


# ---------------------------------------------------------------------------
# Analysis budget
# ---------------------------------------------------------------------------

# Wallclock budget for the track-listing analysis phase (seconds).
# libpgs is killed if analysis takes longer than this.
LISTING_BUDGET_S = 10.0

# Target display sets per track for analysis.
ANALYSIS_MAX_DS = 125

# Default number of content display sets to render when the user
# accepts the interactive "cached" default (no additional extraction).
DEFAULT_INTERACTIVE_COUNT = 10

# PNG compression level (0-9).  Lower = faster encoding, larger files.
# Level 1 is a good balance for transient inspection PNGs.
PNG_COMPRESS_LEVEL = 1

# Grace period (seconds) after the last track validation before
# restarting libpgs with remaining tracks.  Co-located language tracks
# at the same timestamps produce a burst of display sets in
# microseconds (same MKV cluster); 50 ms is generous while being
# negligible across many restarts.
ANALYSIS_RESTART_GRACE_S = 0.05


class Budget:
    """Lightweight wallclock budget tracker using time.monotonic()."""

    def __init__(self, total_seconds: float):
        self._start = time.monotonic()
        self._total = total_seconds
        self.limit = total_seconds

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

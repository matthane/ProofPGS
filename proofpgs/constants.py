"""Shared constants for the ProofPGS decoder."""

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

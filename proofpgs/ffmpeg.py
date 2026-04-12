"""ffprobe integration: video range detection and track folder naming."""

import json
import shutil
import subprocess


def check_ffprobe() -> str | None:
    """Find ffprobe on PATH.

    Returns the path to ffprobe, or None if not found.
    ffprobe is only needed for probe_video_stream() (mismatch badge).
    """
    return shutil.which("ffprobe")


# Transfer characteristics that indicate HDR or SDR.
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_SDR_TRANSFERS = {
    "bt709", "smpte170m", "bt470m", "bt470bg",
    "gamma22", "gamma28", "iec61966-2-1",
}
# Color primaries that indicate wide color gamut / HDR.
_HDR_PRIMARIES = {"bt2020"}


def probe_video_stream(ffprobe_path: str, input_path: str) -> dict | None:
    """Detect the video stream's dynamic range and resolution.

    Probes video streams and examines the main video stream's
    ``color_transfer`` field.  Falls back to ``color_primaries``
    (BT.2020 -> HDR), then checks for a Dolby Vision configuration
    record in ``side_data_list`` (DV Profile 5 and others may lack
    standard color metadata entirely).  Defaults to SDR -- standard
    Blu-ray rips almost never carry explicit color metadata, while
    HDR standards require signaling.

    Attached pictures (cover art) are skipped.

    Returns a dict ``{"range": "hdr"|"sdr", "width": int, "height": int}``
    or ``None`` (no real video stream).  Width/height may be 0 if the
    stream doesn't report them.  Advisory only -- never raises on failure.
    """
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v", input_path],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    # Filter out attached pictures (cover art, thumbnails).
    streams = [
        s for s in data.get("streams", [])
        if not s.get("disposition", {}).get("attached_pic", 0)
    ]
    if not streams:
        return None

    # Prefer the default video stream; fall back to the first.
    chosen = streams[0]
    for s in streams:
        if s.get("disposition", {}).get("default", 0):
            chosen = s
            break

    width = int(chosen.get("width") or chosen.get("coded_width") or 0)
    height = int(chosen.get("height") or chosen.get("coded_height") or 0)

    # 1. Explicit transfer characteristics (strongest signal).
    transfer = chosen.get("color_transfer", "")
    if transfer in _HDR_TRANSFERS:
        return {"range": "hdr", "width": width, "height": height}
    if transfer in _SDR_TRANSFERS:
        return {"range": "sdr", "width": width, "height": height}

    # 2. Color primaries fallback (BT.2020 implies HDR/WCG).
    primaries = chosen.get("color_primaries", "")
    if primaries in _HDR_PRIMARIES:
        return {"range": "hdr", "width": width, "height": height}

    # 3. Dolby Vision side data — DV Profile 5 (and others) may lack
    #    standard color_transfer/color_primaries metadata entirely.
    for sd in chosen.get("side_data_list", []):
        if sd.get("side_data_type") == "DOVI configuration record":
            return {"range": "hdr", "width": width, "height": height}

    # 4. No HDR indicators — SDR is the default for video content.
    #    SDR Blu-ray rips (H.264/1080p) almost never carry explicit
    #    color metadata; HDR standards require signaling.
    return {"range": "sdr", "width": width, "height": height}


def build_track_folder_name(pgs_index: int, track_info: dict) -> str:
    """Build a subfolder name like 'track_1_ger' or 'track_3_eng_forced'.

    *pgs_index* is the internal 0-based index; the folder name uses the
    1-based display number to match the track listing.
    """
    parts = [f"track_{pgs_index + 1}", track_info["language"]]
    if track_info["forced"]:
        parts.append("forced")
    if track_info["default"]:
        parts.append("default")
    return "_".join(parts)

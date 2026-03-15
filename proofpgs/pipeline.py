"""High-level orchestration: process .sup files and video containers."""

import os
import shutil
import subprocess
import sys
import tempfile

from .parser import read_sup, ds_has_content
from .renderer import process_display_sets
from .detect import detect_from_palettes, format_detection
from .ffmpeg import (
    check_ffmpeg, probe_pgs_tracks,
    extract_all_pgs_tracks, build_track_folder_name,
    extract_track_streaming, extract_analysis_samples,
)
from .interactive import select_tracks_interactive, select_count_interactive
from .constants import Budget, LISTING_BUDGET_S

# Tracks with fewer display sets per minute than this are flagged sparse.
_SPARSE_DS_PER_MIN = 1.0


def _resolve_auto_mode(detection: dict) -> str:
    """Resolve 'auto' mode using a detection result. Returns resolved mode."""
    if detection["verdict"] is not None:
        return detection["verdict"]
    return "compare"


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _analyze_tracks(tracks, track_indices, ffmpeg_path, input_path,
                    duration_s, preview_cache, budget=None):
    """Single-pass analysis: extract samples for all requested tracks at
    once, then run detection and count estimation on each.

    Updates each track dict in-place with:
      detection, num_frames (if not set), estimated, sparse, analysis_bailed
    Caches extracted display sets in *preview_cache*.
    """
    if not track_indices:
        return

    # Determine seek position — mid-file for long files, none for short.
    has_duration = duration_s is not None and duration_s > 0
    is_short = not has_duration or duration_s <= 120
    seek_s = None if is_short else max(0, (duration_s / 2) - 60)

    extract_tracks = [tracks[ti] for ti in track_indices]

    label = "Validating" if budget is None else "Analyzing"
    print(f"  {label} {len(extract_tracks)} PGS track(s)...")

    # --- Single FFmpeg pass for all tracks ---
    temp_dir = tempfile.mkdtemp(prefix="pgs_analysis_")
    try:
        sup_paths = extract_analysis_samples(
            ffmpeg_path, input_path, extract_tracks, temp_dir,
            seek_s=seek_s,
            deadline=budget.deadline() if budget else None,
        )

        for list_i, ti in enumerate(track_indices):
            t = tracks[ti]
            sup_path = sup_paths[list_i]

            if os.path.isfile(sup_path) and os.path.getsize(sup_path) > 0:
                ds = read_sup(sup_path)
            else:
                ds = []

            preview_cache[ti] = ds
            content_count = sum(1 for d in ds if ds_has_content(d))

            # --- Subtitle count estimation ---
            if t["num_frames"] is None:
                if content_count > 0 and not is_short and has_duration:
                    # Extrapolate from PTS span of the gathered data.
                    first_pts = ds[0][0]["pts"] / 90_000.0
                    last_pts = ds[-1][0]["pts"] / 90_000.0
                    pts_span = last_pts - first_pts
                    if pts_span > 0:
                        t["num_frames"] = round(
                            content_count * (duration_s / pts_span)
                        )
                    else:
                        # All DS at same PTS — use raw count.
                        t["num_frames"] = content_count
                    t["estimated"] = True
                elif content_count > 0:
                    t["num_frames"] = content_count
                    t["estimated"] = False

            # --- Color space detection ---
            if ds:
                t["detection"] = detect_from_palettes(ds)
            else:
                t["detection"] = {
                    "verdict": None, "confidence": "low",
                    "max_y": 0, "max_achromatic_y": None,
                    "max_pq_channel": 0, "num_palettes": 0,
                }

            # --- Sparsity ---
            nf = t["num_frames"]
            if nf is not None and has_duration:
                t["sparse"] = nf / (duration_s / 60.0) < _SPARSE_DS_PER_MIN
            elif t["forced"]:
                t["sparse"] = True
            else:
                t["sparse"] = False

            # --- Bail-out ---
            t["analysis_bailed"] = (
                content_count == 0 and t["detection"]["verdict"] is None
            )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # --- Retry inconclusive detections from start of file ---
    if seek_s is not None:
        can_retry = budget is None or not budget.exhausted()
        retry_indices = [
            ti for ti in track_indices
            if tracks[ti]["detection"]["verdict"] is None and can_retry
        ]
        if retry_indices:
            retry_tracks = [tracks[ti] for ti in retry_indices]
            temp_dir2 = tempfile.mkdtemp(prefix="pgs_retry_")
            try:
                sup_paths2 = extract_analysis_samples(
                    ffmpeg_path, input_path, retry_tracks, temp_dir2,
                    seek_s=None,  # from start of file
                    deadline=budget.deadline() if budget else None,
                    duration_s=duration_s if budget is None else None,
                )
                for list_i, ti in enumerate(retry_indices):
                    t = tracks[ti]
                    sup_path = sup_paths2[list_i]
                    if (os.path.isfile(sup_path)
                            and os.path.getsize(sup_path) > 0):
                        ds = read_sup(sup_path)
                        fallback = detect_from_palettes(ds)
                        if fallback["verdict"] is not None:
                            t["detection"] = fallback
                        # Update cache / counts if we now have data
                        # for a previously-bailed track.
                        content_count = sum(
                            1 for d in ds if ds_has_content(d)
                        )
                        if content_count > 0:
                            preview_cache[ti] = ds
                            if t["num_frames"] is None:
                                t["num_frames"] = content_count
                                t["estimated"] = True
                            t["analysis_bailed"] = False
            finally:
                shutil.rmtree(temp_dir2, ignore_errors=True)


def _print_track_listing(tracks):
    """Print the track listing with analysis results.

    Returns True if any tracks were bailed (not analyzed).
    """
    has_sparse = False
    has_bailed = False

    print(f"Found {len(tracks)} PGS subtitle track(s):")
    for ti, t in enumerate(tracks):
        flags = []
        if t["forced"]:  flags.append("forced")
        if t["default"]: flags.append("default")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        title_str = f'  "{t["title"]}"' if t["title"] else ""

        if t.get("analysis_bailed"):
            has_bailed = True
            extra = "  [not analyzed — too few samples]"
            print(f"  [{ti}] stream {t['index']}: "
                  f"{t['language']}{title_str}{flag_str}{extra}")
            continue

        # Subtitle count
        if t["num_frames"] is not None:
            if t.get("estimated"):
                n = t['num_frames']
                n = n // 100 * 100 if n > 100 else n // 10 * 10
                count_str = f"  (~{n} subtitles est.)"
            else:
                count_str = f"  (~{t['num_frames']} subtitles)"
        else:
            count_str = ""

        # Detection
        det = t.get("detection", {})
        if det.get("verdict"):
            det_str = f"  [Mastered for {det['verdict'].upper()}]"
        else:
            det_str = ""

        # Sparse warning
        slow_warn = ""
        if t.get("sparse"):
            slow_warn = "  ** sparse — may be slow"
            has_sparse = True

        print(f"  [{ti}] stream {t['index']}: "
              f"{t['language']}{title_str}{flag_str}{count_str}"
              f"{det_str}{slow_warn}")

    if has_sparse:
        print()
        print("  Note: Tracks marked 'sparse' contain very few subtitles")
        print("  spread across the movie. Extracting them requires reading")
        print("  much further into the file and will be significantly slower.")
    print()

    return has_bailed


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def process_sup_file(sup_path: str, out_dir: str, mode: str,
                     tonemap: str, first, nocrop: bool) -> int:
    """Decode a .sup file and write PNGs to out_dir. Returns images saved."""
    display_sets = read_sup(sup_path)
    total = sum(1 for ds in display_sets if ds_has_content(ds))
    print(f"  Found {total} subtitle display sets ({len(display_sets)} total incl. clears).")

    # Color space detection
    detection = detect_from_palettes(display_sets)
    det_str = format_detection(detection)
    print(f"  Detected: {det_str}")

    if mode == "validate":
        return 0

    if mode == "auto":
        mode = _resolve_auto_mode(detection)
        print(f"  Mode: {mode.upper()} (auto-detected)  |  Tonemap: {tonemap}  |  Output: {out_dir}/")
    else:
        if (detection["verdict"] is not None
                and detection["verdict"] != mode
                and mode in ("hdr", "sdr")):
            print(f"  WARNING: --mode {mode} specified but {detection['verdict'].upper()} "
                  f"content detected. Subtitles may appear incorrect.")
        print(f"  Mode: {mode.upper()}  |  Tonemap: {tonemap}  |  Output: {out_dir}/")

    return process_display_sets(display_sets, out_dir, mode, tonemap, nocrop,
                                limit=first, detection=detection)


def process_container(input_path: str, out_dir: str, mode: str,
                      tonemap: str, first, nocrop: bool,
                      tracks_arg: str = None) -> None:
    """Extract and decode PGS tracks from a video container.

    When a display-set limit is active (--first or interactive default),
    uses streaming extraction: FFmpeg pipes each track's PGS data to
    stdout and is terminated early once enough display sets are collected.
    This avoids reading the entire container file and creates no temp files.

    When processing all display sets, uses batch extraction to temp files
    (single FFmpeg pass for all selected tracks) for maximum efficiency.
    """
    ffmpeg_path, ffprobe_path = check_ffmpeg()

    print(f"Probing: {input_path}")
    tracks, duration_s = probe_pgs_tracks(ffprobe_path, input_path)

    if not tracks:
        print("No PGS subtitle tracks found.")
        return

    # === Phase 2: Single-pass analysis (target: <10s wallclock) ===
    preview_cache = {}  # ti -> list of display sets
    all_indices = list(range(len(tracks)))

    if mode == "validate":
        _analyze_tracks(tracks, all_indices, ffmpeg_path, input_path,
                        duration_s, preview_cache, budget=None)
    else:
        budget = Budget(LISTING_BUDGET_S)
        _analyze_tracks(tracks, all_indices, ffmpeg_path, input_path,
                        duration_s, preview_cache, budget=budget)

    # === Phase 3: Display track listing ===
    # Clear the "Validating/Analyzing" status line now that results are ready.
    print("\033[A\033[K", end="", flush=True)
    has_bailed = _print_track_listing(tracks)

    if mode == "validate":
        return

    # === Phase 4: Track selection (with [v] validate for bailed tracks) ===
    if tracks_arg is not None:
        if tracks_arg.lower() == "all":
            selected_indices = list(range(len(tracks)))
        else:
            try:
                selected_indices = [int(x.strip()) for x in tracks_arg.split(",")]
                selected_indices = [i for i in selected_indices if 0 <= i < len(tracks)]
            except ValueError:
                selected_indices = list(range(len(tracks)))
            if not selected_indices:
                print("  No valid track numbers. Processing all tracks.")
                selected_indices = list(range(len(tracks)))
    elif sys.stdin.isatty():
        while True:
            any_bailed = any(t.get("analysis_bailed") for t in tracks)
            selection = select_tracks_interactive(tracks, has_bailed=any_bailed)
            if selection == "validate":
                bailed_indices = [
                    i for i, t in enumerate(tracks)
                    if t.get("analysis_bailed")
                ]
                _analyze_tracks(tracks, bailed_indices, ffmpeg_path,
                                input_path, duration_s, preview_cache,
                                budget=None)
                print("\033[A\033[K", end="", flush=True)
                has_bailed = _print_track_listing(tracks)
                continue
            selected_indices = selection
            break
    else:
        selected_indices = list(range(len(tracks)))

    # --- Display-set count ---
    if first is not None:
        max_ds = first
    elif sys.stdin.isatty():
        max_ds = select_count_interactive()
    else:
        max_ds = None  # process all — backward-compatible default

    # --- Resolve auto mode from per-track detection ---
    if mode == "auto":
        verdicts = set()
        for ti in selected_indices:
            v = tracks[ti].get("detection", {}).get("verdict")
            if v:
                verdicts.add(v)

        if len(verdicts) == 1:
            mode = verdicts.pop()
            mode_note = f"{mode.upper()} (auto-detected)"
        elif len(verdicts) > 1:
            mode = "compare"
            mode_note = "COMPARE (mixed color spaces detected across tracks)"
        else:
            mode = "compare"
            mode_note = "COMPARE (detection inconclusive)"
    elif mode in ("hdr", "sdr"):
        mode_note = mode.upper()
        # Warn if any selected track's detection conflicts
        for ti in selected_indices:
            det = tracks[ti].get("detection", {})
            if det.get("verdict") and det["verdict"] != mode:
                print(f"  WARNING: --mode {mode} specified but track {ti} "
                      f"detected as {det['verdict'].upper()}. "
                      f"Subtitles may appear incorrect.")
    else:
        mode_note = mode.upper()

    print()
    track_desc = ", ".join(str(i) for i in selected_indices)
    count_desc = str(max_ds) if max_ds is not None else "all"
    print(f"Processing track(s) [{track_desc}], {count_desc} display set(s) each.")
    print(f"Mode: {mode_note}  |  Tonemap: {tonemap}  |  Output: {out_dir}/")
    print()

    # === Phase 5: Extraction & rendering ===

    total_saved = 0

    if max_ds is not None:
        # ---- Streaming path: pipe per track, stop early ----
        mid_seek = None
        if duration_s and duration_s > 0:
            mid_seek = max(0, (duration_s / 2) - 60)

        for ti in selected_indices:
            track = tracks[ti]
            folder_name = build_track_folder_name(ti, track)
            track_out = os.path.join(out_dir, folder_name)

            title_str = f' "{track["title"]}"' if track["title"] else ""
            sparse_str = ("  [sparse — may need to read far into file]"
                          if track.get("sparse") else "")
            print(f"=== Track {ti}: {track['language']}{title_str} "
                  f"(stream {track['index']}) ==={sparse_str}")

            # Reuse cached analysis data when it has enough content.
            cached = preview_cache.get(ti)
            content_ds = ([d for d in cached if ds_has_content(d)]
                          if cached else [])
            if len(content_ds) >= max_ds:
                display_sets = cached
            else:
                try:
                    display_sets = extract_track_streaming(
                        ffmpeg_path, input_path, track["index"], max_ds,
                        seek_s=mid_seek,
                    )
                except Exception as e:
                    print(f"  [error] Streaming extraction failed: {e}")
                    continue

            if not display_sets:
                print("  No display sets found.")
                continue

            print(f"  Collected {len(display_sets)} display set(s).")
            saved = process_display_sets(
                display_sets, track_out, mode, tonemap, nocrop,
                limit=max_ds,
                detection=tracks[ti].get("detection"),
            )
            total_saved += saved
            print()

    else:
        # ---- Batch path: extract selected tracks to temp files ----
        selected_track_list = [tracks[i] for i in selected_indices]
        temp_dir = tempfile.mkdtemp(prefix="pgs_extract_")

        try:
            try:
                sup_paths = extract_all_pgs_tracks(
                    ffmpeg_path, input_path, selected_track_list,
                    temp_dir, duration_s
                )
            except subprocess.CalledProcessError as e:
                print(f"[error] ffmpeg extraction failed: {e}")
                return

            for enum_i, ti in enumerate(selected_indices):
                track = tracks[ti]
                folder_name = build_track_folder_name(ti, track)
                track_out = os.path.join(out_dir, folder_name)
                temp_sup = sup_paths[enum_i]

                title_str = f' "{track["title"]}"' if track["title"] else ""
                print(f"=== Track {ti}: {track['language']}{title_str} "
                      f"(stream {track['index']}) ===")

                if not os.path.isfile(temp_sup):
                    print(f"  [warn] Extraction produced no output "
                          f"for stream {track['index']}")
                    continue

                saved = process_sup_file(
                    temp_sup, track_out, mode, tonemap, None, nocrop
                )
                total_saved += saved
                print()

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"Done. {total_saved} total images across "
          f"{len(selected_indices)} track(s) in {out_dir}/")

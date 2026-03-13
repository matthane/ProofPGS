"""High-level orchestration: process .sup files and video containers."""

import os
import shutil
import subprocess
import sys
import tempfile

from .parser import read_sup, ds_has_content
from .renderer import process_display_sets
from .ffmpeg import (
    check_ffmpeg, probe_pgs_tracks, SAMPLE_SECONDS,
    extract_all_pgs_tracks, build_track_folder_name,
    extract_track_streaming,
)
from .interactive import select_tracks_interactive, select_count_interactive


def process_sup_file(sup_path: str, out_dir: str, mode: str,
                     tonemap: str, first, nocrop: bool) -> int:
    """Decode a .sup file and write PNGs to out_dir. Returns images saved."""
    display_sets = read_sup(sup_path)
    total = sum(1 for ds in display_sets if ds_has_content(ds))
    print(f"  Found {total} subtitle display sets ({len(display_sets)} total incl. clears).")
    print(f"  Mode: {mode.upper()}  |  Tonemap: {tonemap}  |  Output: {out_dir}/")

    return process_display_sets(display_sets, out_dir, mode, tonemap, nocrop,
                                limit=first)


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

    # For tracks missing element counts (non-MKV containers), extract
    # from a 2-minute window in the middle of the file.  The -t flag
    # limits FFmpeg's read to that window — no max_ds cap — so we get
    # every display set within the window for an accurate density
    # estimate.  The extracted data is cached and reused for rendering
    # when the requested max_ds fits within what we already have.
    preview_cache = {}  # ti -> list of display sets

    needs_count = [ti for ti, t in enumerate(tracks) if t["num_frames"] is None]
    if needs_count and duration_s and duration_s > 0:
        midpoint = max(0, (duration_s - SAMPLE_SECONDS) / 2)
        # For short files the window covers the whole duration.
        window = min(SAMPLE_SECONDS, duration_s)
        is_estimated = window < duration_s

        for ti in needs_count:
            t = tracks[ti]
            try:
                ds = extract_track_streaming(
                    ffmpeg_path, input_path, t["index"],
                    # No max_ds cap — the -t duration flag limits the
                    # read window.  We want ALL display sets within the
                    # window for an accurate count / density estimate.
                    seek_s=midpoint if is_estimated else None,
                    read_duration_s=window if is_estimated else None,
                )
            except Exception:
                ds = []

            preview_cache[ti] = ds
            content_count = sum(1 for d in ds if ds_has_content(d))

            if is_estimated:
                # Extrapolate from the sample window to the full duration.
                t["num_frames"] = round(
                    content_count * (duration_s / window)
                )
            else:
                # Short file, read the whole thing — count is exact.
                t["num_frames"] = content_count
            t["estimated"] = is_estimated

    # Determine which tracks are sparse.  num_frames is in display-set
    # units (MKV's NUMBER_OF_FRAMES already counts at that level; for
    # other containers the pre-extraction above counts content display
    # sets directly).  We treat anything under 1 display set per minute
    # as "sparse".
    SPARSE_DS_PER_MIN = 1.0   # threshold: fewer -> sparse warning

    has_sparse = False
    sparse_set = set()

    for ti, t in enumerate(tracks):
        nf = t["num_frames"]
        if nf is not None and duration_s and duration_s > 0:
            ds_per_min = nf / (duration_s / 60.0)
            if ds_per_min < SPARSE_DS_PER_MIN:
                sparse_set.add(ti)
                has_sparse = True
        elif t["forced"]:
            # No frame count available — fall back to forced flag
            sparse_set.add(ti)
            has_sparse = True

    print(f"Found {len(tracks)} PGS subtitle track(s):")
    for ti, t in enumerate(tracks):
        flags = []
        if t["forced"]:  flags.append("forced")
        if t["default"]: flags.append("default")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        title_str = f'  "{t["title"]}"' if t["title"] else ""
        # num_frames is normalised to display-set units.
        if t["num_frames"] is not None:
            if t.get("estimated"):
                count_str = f"  (~{t['num_frames']} subtitles est.)"
            else:
                count_str = f"  (~{t['num_frames']} subtitles)"
        else:
            count_str = ""
        slow_warn = "  ** sparse — may be slow" if ti in sparse_set else ""
        print(f"  [{ti}] stream {t['index']}: "
              f"{t['language']}{title_str}{flag_str}{count_str}{slow_warn}")

    if has_sparse:
        print()
        print("  Note: Tracks marked 'sparse' contain very few subtitles")
        print("  spread across the movie. Extracting them requires reading")
        print("  much further into the file and will be significantly slower.")
    print()

    # --- Track selection ---
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
        selected_indices = select_tracks_interactive(tracks)
    else:
        selected_indices = list(range(len(tracks)))

    # --- Display-set count ---
    if first is not None:
        max_ds = first
    elif sys.stdin.isatty():
        max_ds = select_count_interactive()
    else:
        max_ds = None  # process all — backward-compatible default

    print()
    track_desc = ", ".join(str(i) for i in selected_indices)
    count_desc = str(max_ds) if max_ds is not None else "all"
    print(f"Processing track(s) [{track_desc}], {count_desc} display set(s) each.")
    print(f"Mode: {mode.upper()}  |  Tonemap: {tonemap}  |  Output: {out_dir}/")
    print()

    total_saved = 0

    if max_ds is not None:
        # ---- Streaming path: pipe per track, stop early ----
        # No temp files. FFmpeg only reads as far into the container
        # as needed to reach the requested number of subtitles.
        # Extracts from the middle of the file for a representative
        # preview.  If a track was pre-extracted and the cache has
        # enough display sets, skip the extraction entirely.
        mid_seek = None
        if duration_s and duration_s > 0:
            mid_seek = max(0, (duration_s / 2) - 60)

        for ti in selected_indices:
            track = tracks[ti]
            folder_name = build_track_folder_name(ti, track)
            track_out = os.path.join(out_dir, folder_name)

            title_str = f' "{track["title"]}"' if track["title"] else ""
            sparse_str = "  [sparse — may need to read far into file]" \
                if ti in sparse_set else ""
            print(f"=== Track {ti}: {track['language']}{title_str} "
                  f"(stream {track['index']}) ==={sparse_str}")

            # Reuse cached mid-file preview when it has enough data.
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
            )
            total_saved += saved
            print()

    else:
        # ---- Batch path: extract selected tracks to temp files ----
        # Single FFmpeg pass reads the whole container once.
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

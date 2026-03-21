# Plan: Integrate libpgs into ProofPGS

## Context

ProofPGS currently handles PGS file parsing and container extraction through three separate code paths: a Python binary parser (`parser.py`), FFmpeg subprocess calls (`ffmpeg.py`), and a 50KB Matroska direct reader (`mkv.py`). A new Rust CLI tool, [libpgs](https://github.com/matthane/libpgs), has been created to handle all PGS file I/O — both `.sup` files and containers — with a single unified NDJSON streaming interface. This integration replaces all three extraction paths with libpgs, keeping ProofPGS focused on color math, detection, rendering, and user interaction.

## libpgs NDJSON format (updated)

**Tracks header** (first line):
```json
{"type":"tracks","tracks":[{
  "track_id":3, "language":"eng", "container":"Matroska",
  "name":"English", "flag_default":true, "flag_forced":false,
  "display_set_count":1234
}]}
```

**Display set** (subsequent lines):
```json
{"type":"display_set","track_id":3,"index":0,"pts":311580,"pts_ms":3462.0,
 "composition_state":"EpochStart","segments":[
   {"type":"PresentationComposition","pts":311580,"dts":0,"size":19,"payload":"<base64>"},
   {"type":"PaletteDefinition","pts":311580,"dts":0,"size":52,"payload":"<base64>"},
   {"type":"ObjectDefinition","pts":311580,"dts":0,"size":1024,"payload":"<base64>"},
   {"type":"EndOfDisplaySet","pts":311580,"dts":0,"size":0,"payload":""}
]}
```

Segment payloads are base64-encoded raw binary — identical to what the Python parser currently stores as `seg["payload"]`. No `--limit` flag; consumer closes pipe to stop early (BrokenPipe handled gracefully).

---

## What changes

| Component | Action |
|---|---|
| `libpgs.py` | **New** — adapter module wrapping libpgs CLI |
| `pipeline.py` | **Major rewrite** — replace all extraction with libpgs calls |
| `parser.py` | **Trim** — remove file readers, keep payload parsers |
| `ffmpeg.py` | **Trim** — remove extraction functions, keep `probe_video_range()` + `build_track_folder_name()` |
| `mkv.py` | **Delete** — entirely replaced by libpgs |
| `constants.py` | **Trim** — remove `MATROSKA_EXTENSIONS`, `TS_SEGMENTS_PER_DS` |
| `__init__.py` | **Update** — remove `read_sup`, `read_sup_streaming` exports |
| `cli.py` | **Minor** — check for libpgs at startup |

## What stays unchanged

- `color.py`, `detect.py`, `renderer.py` — all color/detection/rendering logic
- `interactive.py`, `style.py`, `shellmenu.py` — UI and terminal styling

---

## Implementation

### Step 1: Create `proofpgs/libpgs.py`

New adapter module. All interaction with the libpgs binary goes through here.

**`check_libpgs() -> str`**
Find the bundled `libpgs` binary. Search order:
1. `proofpgs/bin/libpgs` (or `libpgs.exe` on Windows) — bundled with the project
2. Fall back to `shutil.which("libpgs")` on PATH

The `proofpgs/bin/` directory houses the bundled binary. It should be added to `.gitignore` (platform-specific binaries shouldn't be committed). Exit with error if not found in either location.

**Segment type mapping:**
```python
_SEG_TYPE_MAP = {
    "PresentationComposition": 0x16,
    "WindowDefinition":        0x17,
    "PaletteDefinition":       0x14,
    "ObjectDefinition":        0x15,
    "EndOfDisplaySet":         0x80,
}
```

**`discover_tracks(libpgs_path, input_path) -> list[dict]`**
Spawn `libpgs stream <file>`, read only the first NDJSON line (tracks header), kill the process. Returns list of track dicts with all metadata:
```python
{"track_id": int, "language": str|None, "container": str,
 "name": str|None, "flag_default": bool|None, "flag_forced": bool|None,
 "display_set_count": int|None}
```

**`stream_file(libpgs_path, input_path, track_id=None, max_ds=None, show_progress=False) -> list[list[dict]]`**
Core streaming function. Spawns `libpgs stream <file> [-t <track_id>]`, reads NDJSON lines, converts each display_set into internal format `[{"type": int, "pts": int, "payload": bytes}, ...]`.
- Skips the tracks header line
- Counts content display sets (those with ODS segments) toward `max_ds`
- Closes pipe when limit reached
- Shows progress when `show_progress=True`
- stderr → `subprocess.DEVNULL`

**`stream_all_tracks(libpgs_path, input_path, max_ds_per_track=None, deadline=None, ready_check=None) -> dict[int, list]`**
Stream all tracks from a single `libpgs stream` invocation. Demultiplex by `track_id`. Per-track `max_ds` limits. Early termination via `deadline` (monotonic timestamp) or `ready_check` callback. Returns `{track_id: [display_sets]}`.

### Step 2: Modify `pipeline.py`

**Remove imports:** `read_sup`, `read_sup_streaming` from parser; `extract_all_pgs_tracks`, `extract_track_streaming`, `extract_analysis_samples` from ffmpeg; all `mkv.py` imports; `MATROSKA_EXTENSIONS`, `TS_SEGMENTS_PER_DS` from constants.

**Add imports:** `check_libpgs`, `stream_file`, `discover_tracks`, `stream_all_tracks` from libpgs.

**Track metadata — replace ffprobe with libpgs:**
- Call `discover_tracks()` to get all track metadata (language, name, forced, default, display_set_count, track_id)
- Build track dicts directly from libpgs data — no more ffprobe for track discovery
- Map libpgs fields to current track dict format:
  - `track_id` → stored as `track_id` (used for libpgs `-t` flag) AND as `index` (used for display in track listing)
  - `language` → `language` (default `"und"` if None)
  - `name` → `title`
  - `flag_forced` → `forced` (default False if None)
  - `flag_default` → `default` (default False if None)
  - `display_set_count` → `num_frames`

**Video range detection stays via ffprobe:**
- `probe_video_range()` still called for the mismatch badge
- Only ffprobe dependency remaining — and only needed for containers

**`process_sup_file()` change:**
- Replace `read_sup(sup_path)` with `stream_file(libpgs_path, sup_path)`
- Remove `_adjust_pts_offset()` call — libpgs handles PTS consistently
- Everything else unchanged

**`_analyze_tracks()` rewrite:**
- Replace entire body: FFmpeg/MKV extraction → `stream_all_tracks()`
- Remove: temp directory, `_check_all_detected()`, MKV fast-path, FFmpeg fallback
- `ready_check` callback: runs `detect_from_palettes()` on each track's accumulated data, returns True when all tracks have conclusive verdicts
- Timer display thread stays (user-facing progress)

**Container extraction (Phase 5) simplification:**
- **Streaming path** (max_ds set): check cache first, then `stream_file(..., track_id=..., max_ds=...)`
- **Batch path** (all subtitles): `stream_file(..., track_id=...)` per track, no limit
- Remove: all MKV/FFmpeg extraction branches, temp file management, `shutil.rmtree`

**`_adjust_pts_offset()` — evaluate if still needed:**
- For M2TS containers, PTS from libpgs may still be absolute transport stream timestamps
- Need to verify: if libpgs PTS matches raw .sup PTS (zero-based), offset adjustment can be removed
- If not, keep `start_time_s` via a separate ffprobe call or compute from first PTS

### Step 3: Modify `parser.py`

**Remove:**
- `read_sup()` — replaced by libpgs
- `read_sup_streaming()` — replaced by libpgs

**Keep:**
- `ds_has_content()` — used throughout
- `parse_pcs()`, `parse_pds()`, `parse_ods()`, `decode_rle()` — used by renderer and detect
- `pts_to_ms()` — used by renderer

### Step 4: Modify `ffmpeg.py`

**Remove:**
- `check_ffmpeg()` — no longer need ffmpeg binary
- `probe_pgs_tracks()` — replaced by libpgs `discover_tracks()`
- `extract_all_pgs_tracks()` — replaced by libpgs streaming
- `extract_analysis_samples()` — replaced by libpgs streaming
- `extract_track_streaming()` — replaced by libpgs streaming
- `_HDR_TRANSFERS`, `_SDR_TRANSFERS`, `_HDR_PRIMARIES` — move inline or keep

**Keep:**
- `probe_video_range()` — still needed for mismatch badge (only ffprobe use remaining)
- `build_track_folder_name()` — unchanged

**Add:**
- `check_ffprobe() -> str | None` — find only ffprobe on PATH (ffmpeg no longer needed). Return None if not found (video range is advisory, not required).

### Step 5: Delete `mkv.py`

Remove entirely. All functions (`extract_analysis_samples_mkv`, `extract_pgs_tracks_mkv`, `probe_mkv_subtitle_cues`) replaced by libpgs.

### Step 6: Update `constants.py`

**Remove:**
- `MATROSKA_EXTENSIONS` — MKV fast-path gone
- `TS_SEGMENTS_PER_DS` — M2TS packet scaling handled by libpgs

**Keep:**
- `ANALYSIS_MAX_DS` — controls analysis sample size
- `LISTING_BUDGET_S`, `Budget` — analysis budget
- `DEFAULT_INTERACTIVE_COUNT` — interactive default
- `SEG_*` constants — used by parser payload functions and renderer
- `format_time()` — used for progress display

### Step 7: Update `__init__.py`

Remove `read_sup` and `read_sup_streaming` from exports (no longer public API).

### Step 8: Create `proofpgs/bin/` and update `.gitignore`

Create the `proofpgs/bin/` directory (with a `.gitkeep` to preserve it in git). Add `proofpgs/bin/*` and `!proofpgs/bin/.gitkeep` to `.gitignore` so the directory structure is tracked but platform-specific binaries are not.

### Step 9: Update `cli.py`

Add libpgs check early in `_main()` — before dispatching to `process_sup_file` or `process_container`. Call `check_libpgs()` and pass the path down.

---

## Key design decisions

1. **libpgs is mandatory** — required for both .sup files and containers (user will add .sup support to libpgs first)
2. **Bundled binary** — libpgs binary lives in `proofpgs/bin/`. `check_libpgs()` looks there first, falls back to PATH. The `bin/` directory is gitignored (platform-specific).
3. **No temp files** — all extraction streams into memory via NDJSON. PGS data is small.
4. **ffprobe only for video range** — all track metadata now comes from libpgs. ffprobe is only needed for `probe_video_range()` (mismatch badge). If ffprobe is missing, skip the mismatch badge gracefully.
5. **No track ID mapping needed** — since we no longer use ffprobe for track discovery, we use libpgs track_ids directly everywhere. The track listing shows them as `[0]`, `[1]`, etc. (local indices) with `track_id` available internally for the `-t` flag.
6. **PTS handling** — libpgs PTS is 90kHz ticks, same as internal format. Need to verify M2TS offset behavior.

---

## Files to modify

| File | Path |
|---|---|
| New | `proofpgs/libpgs.py` |
| New | `proofpgs/bin/` (directory for bundled libpgs binary) |
| Modify | `proofpgs/pipeline.py` |
| Modify | `proofpgs/parser.py` |
| Modify | `proofpgs/ffmpeg.py` |
| Modify | `proofpgs/constants.py` |
| Modify | `proofpgs/__init__.py` |
| Modify | `proofpgs/cli.py` |
| Modify | `.gitignore` (add `proofpgs/bin/`) |
| Delete | `proofpgs/mkv.py` |

---

## Verification

1. Process a .sup file → compare PNG output to baseline (byte-identical)
2. Process an MKV container → verify track listing shows correct metadata (language, title, forced, default, subtitle count), detection verdicts, and PNG output
3. Process an M2TS container → verify PTS timestamps are correct in output filenames
4. Test without libpgs on PATH → verify clean error message and exit
5. Test `--first N` → verify pipe closes early, exactly N subtitles rendered
6. Test `--mode validate` / `validate-fast` → verify analysis with budget works
7. Test multi-track container → verify per-track detection and mode resolution
8. Test video range mismatch badge → verify it appears when subtitle and video ranges differ
9. Test without ffprobe on PATH → verify everything works except mismatch badge is silently skipped

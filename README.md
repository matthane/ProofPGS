<img src="proofpgs/assets/proofpgs-icon-light.png" width="64" height="64" alt="ProofPGS icon">

# ProofPGS

A tool for inspecting and exporting PGS (Presentation Graphic Stream) subtitles. Validates PGS tracks with per-track SDR/HDR detection and can export each subtitle as a PNG using the correct colour pipeline — **HDR** (UHD Blu-ray, BT.2020 + PQ) or **SDR** (standard Blu-ray, BT.709).

Accepts `.sup` files directly, or video containers (MKV, MK3D, M2TS) from which PGS subtitle tracks are automatically discovered and extracted via [libpgs](https://github.com/matthane/libpgs).

## Installation

Download the latest release for your platform from the [Releases](https://github.com/matthane/ProofPGS/releases) page. Each release archive includes ProofPGS and a pre-built [libpgs](https://github.com/matthane/libpgs) binary — no separate download needed.

### Requirements

- Python 3.10+
- [libpgs](https://github.com/matthane/libpgs) (bundled in release archives — only needed separately when running from source)
- [NumPy](https://pypi.org/project/numpy/)
- [Pillow](https://pypi.org/project/Pillow/)
- [FFmpeg](https://ffmpeg.org/) (optional — only needed for the video stream dynamic range mismatch badge)

Install Python dependencies:

```
pip install numpy pillow
```

### Running from source

If you prefer to run from a git clone instead of a release archive, you'll need to provide the [libpgs](https://github.com/matthane/libpgs) binary yourself. Place `libpgs` (or `libpgs.exe` on Windows) in `proofpgs/bin/`, or add it to your system PATH.

## Quick Start

Just point ProofPGS at any video file or `.sup` file and run it from the project root:

```bash
python -m proofpgs movie.mkv
```

That's it. ProofPGS will:

1. Detect all PGS subtitle tracks in the file
2. Auto-detect whether each track is SDR or HDR (per-track color space detection), flagging any mismatch with the video stream's dynamic range
3. Prompt you to pick which tracks to process (with an option to validate sparse tracks)
4. Prompt you for how many subtitles to decode (defaults to up to 10 cached analysis samples for instant output)
5. Decode using the correct color pipeline and save PNGs to a `<filename>_pgs_output/` folder next to the input file

Works the same way with `.sup` and `.m2ts` files.

## Advanced Usage

```
python -m proofpgs <input_file> [options]
```

### Skip the prompts (non-interactive)

```bash
# Specific tracks, first 20 subtitles:
python -m proofpgs movie.mkv --tracks 0,2 --first 20

# All tracks, all subtitles:
python -m proofpgs movie.mkv --tracks all

# Custom output directory:
python -m proofpgs movie.mkv --out ./my_output
```

### Extract a specific time range

Use `--start` and `--end` to extract subtitles from a specific portion of the file. libpgs seeks directly to the target offset — data before the start point is not read.

```bash
# Subtitles from 5 minutes onward:
python -m proofpgs movie.mkv --start 0:05:00

# Subtitles between 1:30:00 and 1:35:00:
python -m proofpgs movie.mkv --start 1:30:00 --end 1:35:00

# First 10 subtitles within a time window:
python -m proofpgs movie.mkv --start 0:05:00 --end 0:10:00 --first 10
```

Timestamps accept `HH:MM:SS.ms`, `MM:SS.ms`, `SS.ms`, or plain seconds (e.g. `300`). `--start` and `--end` can be used independently or together, and compose with `--first`.

### Output modes

ProofPGS has six output modes:

- **`auto`** (default) — Automatically detects whether each subtitle track was mastered for SDR or HDR by analyzing palette data, then decodes each track with the correct pipeline independently. A container with mixed SDR and HDR tracks will process each track using its own detected color space. Falls back to `compare` for any individual track where detection is inconclusive.
- **`compare`** — For delivery proofing. Produces an annotated PNG with a dark background showing the SDR and HDR decodes side by side, labelled for easy comparison. These are opaque RGB images meant for visual review.
- **`hdr`** — Direct export. Outputs the HDR (BT.2020+PQ) decode as a transparent PNG, cropped to content. Useful when you need the subtitle graphic itself.
- **`sdr`** — Direct export. Outputs the SDR (BT.709) decode as a transparent PNG, cropped to content.
- **`validate`** — Analyzes all tracks without a time limit (with scan progress) and displays track information and SDR/HDR detection results without producing any output. Useful for thoroughly checking what PGS tracks a file contains and whether they are mastered for SDR or HDR, including sparse tracks that may be skipped during normal interactive analysis.
- **`validate-fast`** — Runs the same analysis as `validate` but under the normal 10-second wallclock budget. Sparse tracks that can't be analyzed in time are flagged, and you're prompted to re-analyze them without a time limit if desired. Useful for a quick check when a full unbounded scan isn't needed.

```bash
# Auto-detect color space and decode accordingly (default):
python -m proofpgs input.sup

# Force side-by-side comparison:
python -m proofpgs input.sup --mode compare

# Direct export — transparent HDR-decoded PNGs:
python -m proofpgs input.sup --mode hdr

# Direct export — transparent SDR-decoded PNGs:
python -m proofpgs input.sup --mode sdr

# Show track info and detection only (no output):
python -m proofpgs movie.mkv --mode validate

# Quick validation under 10s budget (prompts to re-analyze sparse tracks):
python -m proofpgs movie.mkv --mode validate-fast
```

## Options

| Option | Values | Default | Description |
|---|---|---|---|
| `--mode` | `auto`, `compare`, `hdr`, `sdr`, `validate`, `validate-fast` | `auto` | `auto` detects color space per-track and decodes each track independently with the correct pipeline. `compare` produces annotated side-by-side proofing images. `hdr` and `sdr` produce direct transparent PNG exports. `validate` shows track info and detection only (no output). `validate-fast` same as validate but under the 10s analysis budget with option to re-analyze sparse tracks. |
| `--tonemap` | `clip`, `reinhard` | `clip` | HDR-to-SDR tonemapping strategy. `clip` hard-clips at 203 nits reference white (best for subtitles). `reinhard` applies a soft roll-off. |
| `--out` | path | `<filename>_pgs_output/` next to input file | Output directory. |
| `--first` | integer | all | Decode only the first N subtitle display sets. |
| `--start` | timestamp | beginning | Start timestamp for extraction (e.g. `0:05:00`, `5:00`, `300`). Seeks directly to the target offset. |
| `--end` | timestamp | end of file | End timestamp for extraction (e.g. `0:10:00`, `10:00`, `600`). |
| `--tracks` | e.g. `0,2,3` or `all` | interactive | Which PGS tracks to process (container input only). |
| `--nocrop` | flag | off | Output full video-frame-sized PNGs instead of cropping to subtitle content. |
| `--threads` | integer | auto (up to 8) | Number of parallel rendering threads. |
| `--install` | flag | — | Register Windows Explorer context menu entries for all supported file types. |
| `--uninstall` | flag | — | Remove Windows Explorer context menu entries. |

## Windows Explorer Integration

ProofPGS can add a right-click context menu for all supported file types (`.sup`, `.mkv`, `.mk3d`, `.m2ts`). The menu shows a **ProofPGS** submenu with entries for each output mode.

```bash
# Register context menu entries:
python -m proofpgs --install

# Remove context menu entries:
python -m proofpgs --uninstall
```

The install command records the paths to both the Python interpreter and the project directory at install time. If you move the project or switch Python environments, run `--install` again to update the paths.

On Windows 11, right-click a supported file and choose **Show more options** to see the ProofPGS submenu.

## Output

Each subtitle is saved as a PNG file named with its display set index, timestamp, and decoded color space:

```
movie_pgs_output/
  track_0_eng/
    ds_0000_12500ms_sdr.png
    ds_0001_15200ms_sdr.png
    ...
  track_1_ger_forced/
    ds_0000_8300ms_hdr.png
    ...
```

The output folder is named after the input file (e.g. `movie.mkv` → `movie_pgs_output/`). The range suffix (`_sdr`, `_hdr`, or `_compare`) indicates which color pipeline was used to decode the subtitle.

For `.sup` input (single track), images are written directly to the output directory without a track subfolder.

## Colour Pipeline

### HDR (UHD Blu-ray)

The palette is encoded in BT.2020 primaries with ST 2084 (PQ) transfer function, per the UHD BD specification. ProofPGS applies the full inverse pipeline:

```
BT.2020 YCbCr (limited range)  ->  BT.2020 matrix  ->  PQ EOTF (linearise)
  ->  BT.2020 to BT.709 gamut mapping  ->  Tonemap to SDR
  ->  sRGB gamma  ->  PNG
```

The result is the closest possible SDR/BT.709 representation of the original HDR colour. Brightness above 203 nits reference white is clipped (or soft-mapped with Reinhard), but hue and saturation are preserved.

### SDR (standard Blu-ray)

```
BT.709 YCbCr (limited range)  ->  BT.709 gamma  ->  BT.1886 linearise  ->  sRGB gamma  ->  PNG
```

## Performance

**libpgs extraction:** All file I/O — both `.sup` files and containers (MKV, M2TS) — is handled by [libpgs](https://github.com/matthane/libpgs), a Rust CLI that streams PGS data as NDJSON over a subprocess pipe. For MKV files, libpgs uses the Cues index to seek directly to subtitle blocks, reading only a few MB out of tens of GB for large UHD remuxes. No temp files are created — display sets are streamed into memory as they are extracted.

**Track listing (sub-10s):** When opening a container, ProofPGS analyzes all PGS tracks under a 10-second wallclock budget. libpgs streams display sets from all tracks in a single pass. Tracks that receive enough data get SDR/HDR detection. Sparse tracks (e.g. forced subtitles with very few entries) that can't be analyzed in time are flagged, and you can press `[v]` at the track selection prompt to re-analyze them without a time limit. A content-based watchdog also terminates extraction early once all tracks have conclusive detection, so analysis often finishes well under 10 seconds.

**Streaming extraction:** When processing containers with a display-set limit (`--first` or the interactive default of 10), ProofPGS closes the libpgs pipe once enough display sets have been collected. Analysis data is reused when it already contains enough display sets, avoiding redundant extraction.

**Single-pass multi-track extraction:** When multiple tracks are selected, all tracks are extracted in a single libpgs invocation — regardless of whether the container has MKV Cues. A reader thread demuxes the NDJSON stream into per-track queues consumed by concurrent renderers. For limited extractions (e.g. `--first 100`), the reader enforces per-track limits and terminates the subprocess once all tracks are satisfied. This avoids redundant MKV header / cues parsing that would otherwise cause pauses between tracks, especially on network storage.

**Multi-threaded rendering:** PNG rendering uses multiple threads by default (auto-detected, up to 8). Override with `--threads`.

## Project Structure

```
proofpgs/
  assets/             # Bundled resources (fonts, icons)
    Sora-Medium.ttf
    Sora-Regular.ttf
  bin/                # Bundled libpgs binary (platform-specific, gitignored)
  __init__.py         # Public API exports
  __main__.py         # python -m proofpgs entry point
  cli.py              # Argument parsing and main()
  constants.py        # PQ constants, segment types, file extensions
  detect.py           # SDR/HDR auto-detection via PQ plausibility analysis
  parser.py           # Display set content check (ds_has_content)
  color.py            # Colour-space math and palette decoding (HDR & SDR)
  renderer.py         # Display set rendering and PNG output
  libpgs.py           # libpgs CLI adapter (subprocess streaming)
  ffmpeg.py           # ffprobe video range detection
  interactive.py      # Interactive track and count selection
  pipeline.py         # High-level orchestration
  shellmenu.py        # Windows Explorer context menu integration
  style.py            # Terminal styling and color output
LICENSES/
  OFL.txt             # SIL Open Font License 1.1 (Sora font)
```

## Build Provenance

Release archives are built entirely in GitHub Actions from auditable source code — no locally-built binaries are uploaded. The libpgs binary included in each release is compiled from the [libpgs source](https://github.com/matthane/libpgs) at its latest tagged release using `cargo build --release` on each platform's native CI runner.

Every release archive includes a `BUILD_INFO.txt` with the exact libpgs tag, commit hash, build target, and a link to the workflow run log. Release artifacts are signed with [Sigstore](https://www.sigstore.dev/) artifact attestations, cryptographically linking each archive to the GitHub Actions workflow and source commit that produced it.

To verify a downloaded release:

```bash
gh attestation verify ProofPGS-<version>-<platform>.zip --repo matthane/ProofPGS
```

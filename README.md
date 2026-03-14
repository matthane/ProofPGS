# ProofPGS

A PGS (Presentation Graphic Stream) subtitle decoder that exports each subtitle as a PNG image. Supports both **HDR** (UHD Blu-ray, BT.2020 + PQ) and **SDR** (standard Blu-ray, BT.709) colour pipelines.

Accepts `.sup` files directly, or video containers (MKV, M2TS, etc.) from which PGS subtitle tracks are automatically extracted via FFmpeg.

## Requirements

- Python 3.10+
- [NumPy](https://pypi.org/project/numpy/)
- [Pillow](https://pypi.org/project/Pillow/)
- [FFmpeg](https://ffmpeg.org/) (only needed when processing video containers)

Install Python dependencies:

```
pip install numpy pillow
```

## Quick Start

Just point ProofPGS at any video file or `.sup` file and run it from the project root:

```bash
python -m proofpgs movie.mkv
```

That's it. ProofPGS will:

1. Detect all PGS subtitle tracks in the file
2. Auto-detect whether each track is SDR or HDR (per-track color space detection)
3. Prompt you to pick which tracks to process (with an option to validate sparse tracks)
4. Prompt you for how many subtitles to decode (defaults to 10 for a fast preview)
5. Decode using the correct color pipeline and save PNGs to a `pgs_output/` folder next to the input file

Works the same way with `.sup`, `.m2ts`, `.ts`, `.mp4`, and other container formats.

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

### Output modes

ProofPGS has five output modes:

- **`auto`** (default) — Automatically detects whether each subtitle track was mastered for SDR or HDR by analyzing palette data, then decodes with the correct pipeline. Falls back to `compare` if detection is inconclusive or tracks have mixed color spaces.
- **`compare`** — For delivery proofing. Produces an annotated PNG with a dark background showing the SDR and HDR decodes side by side, labelled for easy comparison. These are opaque RGB images meant for visual review.
- **`hdr`** — Direct export. Outputs the HDR (BT.2020+PQ) decode as a transparent PNG, cropped to content. Useful when you need the subtitle graphic itself.
- **`sdr`** — Direct export. Outputs the SDR (BT.709) decode as a transparent PNG, cropped to content.
- **`validate`** — Analyzes all tracks without a time limit (with scan progress) and displays track information and SDR/HDR detection results without producing any output. Useful for thoroughly checking what PGS tracks a file contains and whether they are mastered for SDR or HDR, including sparse tracks that may be skipped during normal interactive analysis.

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
```

## Options

| Option | Values | Default | Description |
|---|---|---|---|
| `--mode` | `auto`, `compare`, `hdr`, `sdr`, `validate` | `auto` | `auto` detects color space per-track and decodes accordingly. `compare` produces annotated side-by-side proofing images. `hdr` and `sdr` produce direct transparent PNG exports. `validate` shows track info and detection only (no output). |
| `--tonemap` | `clip`, `reinhard` | `clip` | HDR-to-SDR tonemapping strategy. `clip` hard-clips at 203 nits reference white (best for subtitles). `reinhard` applies a soft roll-off. |
| `--out` | path | `pgs_output/` next to input file | Output directory. |
| `--first` | integer | all | Decode only the first N subtitle display sets. |
| `--tracks` | e.g. `0,2,3` or `all` | interactive | Which PGS tracks to process (container input only). |
| `--nocrop` | flag | off | Output full video-frame-sized PNGs instead of cropping to subtitle content. |
| `--install` | flag | — | Register Windows Explorer context menu entries for all supported file types. |
| `--uninstall` | flag | — | Remove Windows Explorer context menu entries. |

## Windows Explorer Integration

ProofPGS can add a right-click context menu for all supported file types (`.sup`, `.mkv`, `.m2ts`, `.ts`, `.mp4`, `.m4v`, `.avi`, `.wmv`). The menu shows a **ProofPGS** submenu with entries for each output mode.

```bash
# Register context menu entries:
python -m proofpgs --install

# Remove context menu entries:
python -m proofpgs --uninstall
```

The install command records the paths to both the Python interpreter and the project directory at install time. If you move the project or switch Python environments, run `--install` again to update the paths.

On Windows 11, right-click a supported file and choose **Show more options** to see the ProofPGS submenu.

## Output

Each subtitle is saved as a PNG file named with its display set index and timestamp:

```
pgs_output/
  track_0_eng/
    ds_0000_12500ms_compare.png
    ds_0001_15200ms_compare.png
    ...
  track_1_ger_forced/
    ...
```

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

**Track listing (sub-10s):** When opening a container, ProofPGS analyzes all PGS tracks in a single FFmpeg pass under a 10-second wallclock budget. It seeks to the middle of the file for representative content and extracts samples from all tracks simultaneously. Tracks that receive enough data get a subtitle count estimate and SDR/HDR detection. Sparse tracks (e.g. forced subtitles with very few entries) that can't be analyzed in time are flagged, and you can press `[v]` at the track selection prompt to re-analyze them without a time limit.

**Streaming extraction:** When processing containers with a display-set limit (`--first` or the interactive default of 10), FFmpeg pipes each track directly to the parser and is terminated as soon as enough subtitles are collected. No temp files are created. The default of 10 samples from the **middle of the file** for representative content.

**Batch extraction:** When processing all subtitles, a single FFmpeg pass extracts all selected tracks to temporary files, then each is decoded in turn.

## Project Structure

```
proofpgs/
  __init__.py       # Public API exports
  __main__.py       # python -m proofpgs entry point
  cli.py            # Argument parsing and main()
  constants.py      # PQ constants, segment types, file extensions
  detect.py         # SDR/HDR auto-detection via PQ plausibility analysis
  parser.py         # PGS binary parsing, segment parsers, RLE decoder
  color.py          # Colour-space math and palette decoding (HDR & SDR)
  renderer.py       # Display set rendering and PNG output
  ffmpeg.py         # FFmpeg/ffprobe integration
  interactive.py    # Interactive track and count selection
  pipeline.py       # High-level orchestration
  shellmenu.py      # Windows Explorer context menu integration
```

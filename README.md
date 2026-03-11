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
2. Prompt you to pick which tracks to process
3. Prompt you for how many subtitles to decode (defaults to 10 for a fast preview)
4. Save side-by-side SDR/HDR comparison PNGs to a `pgs_output/` folder next to the input file

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

ProofPGS has three output modes:

- **`compare`** (default) — For delivery proofing. Produces an annotated PNG with a dark background showing the SDR and HDR decodes side by side, labelled for easy comparison. These are opaque RGB images meant for visual review.
- **`hdr`** — Direct export. Outputs the HDR (BT.2020+PQ) decode as a transparent PNG, cropped to content. Useful when you need the subtitle graphic itself.
- **`sdr`** — Direct export. Outputs the SDR (BT.709) decode as a transparent PNG, cropped to content.

```bash
# Delivery proofing — annotated side-by-side (default):
python -m proofpgs input.sup

# Direct export — transparent HDR-decoded PNGs:
python -m proofpgs input.sup --mode hdr

# Direct export — transparent SDR-decoded PNGs:
python -m proofpgs input.sup --mode sdr
```

## Options

| Option | Values | Default | Description |
|---|---|---|---|
| `--mode` | `compare`, `hdr`, `sdr` | `compare` | `compare` produces annotated side-by-side proofing images. `hdr` and `sdr` produce direct transparent PNG exports. |
| `--tonemap` | `clip`, `reinhard` | `clip` | HDR-to-SDR tonemapping strategy. `clip` hard-clips at 203 nits reference white (best for subtitles). `reinhard` applies a soft roll-off. |
| `--out` | path | `pgs_output/` next to input file | Output directory. |
| `--first` | integer | all | Decode only the first N subtitle display sets. |
| `--tracks` | e.g. `0,2,3` or `all` | interactive | Which PGS tracks to process (container input only). |
| `--nocrop` | flag | off | Output full video-frame-sized PNGs instead of cropping to subtitle content. |

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
Raw YCbCr  ->  BT.2020 matrix  ->  PQ EOTF (linearise)
  ->  BT.2020 to BT.709 gamut mapping  ->  Tonemap to SDR
  ->  sRGB gamma  ->  PNG
```

The result is the closest possible SDR/BT.709 representation of the original HDR colour. Brightness above 203 nits reference white is clipped (or soft-mapped with Reinhard), but hue and saturation are preserved.

### SDR (standard Blu-ray)

```
BT.709 YCbCr  ->  BT.709 gamma  ->  sRGB PNG
```

## Performance

When processing containers with a display-set limit (`--first` or the interactive default of 10), ProofPGS uses **streaming extraction**: FFmpeg pipes each track directly to the parser and is terminated as soon as enough subtitles are collected. Only the portion of the container up to the last needed subtitle is read from disk. No temp files are created in this mode.

When processing all subtitles, a single FFmpeg pass extracts all selected tracks to temporary files, then each is decoded in turn.

## Project Structure

```
proofpgs/
  __init__.py       # Public API exports
  __main__.py       # python -m proofpgs entry point
  cli.py            # Argument parsing and main()
  constants.py      # PQ constants, segment types, file extensions
  parser.py         # PGS binary parsing, segment parsers, RLE decoder
  color.py          # Colour-space math and palette decoding (HDR & SDR)
  renderer.py       # Display set rendering and PNG output
  ffmpeg.py         # FFmpeg/ffprobe integration
  interactive.py    # Interactive track and count selection
  pipeline.py       # High-level orchestration
```

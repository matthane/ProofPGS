# CLAUDE.md — ProofPGS

## What this project does

ProofPGS decodes PGS (Presentation Graphic Stream) subtitles from `.sup` files or video containers and exports each subtitle as a PNG. It supports two colour pipelines: HDR (UHD Blu-ray, BT.2020 + PQ) and SDR (standard Blu-ray, BT.709).

Run from the project root:

```bash
python -m proofpgs <input_file> [options]
```

## Dependencies

- Python 3.10+
- `numpy`, `pillow` — `pip install numpy pillow`
- FFmpeg / ffprobe on PATH (only needed for container input, not `.sup`)

There is no test suite. Validation is done by visual inspection of the output PNGs.

## Module responsibilities

| File | Role |
|---|---|
| `cli.py` | Argument parsing, `main()` entry point |
| `pipeline.py` | High-level orchestration — ties everything together |
| `color.py` | All colour-space math and palette LUT construction |
| `detect.py` | SDR/HDR auto-detection via PQ plausibility analysis |
| `parser.py` | PGS binary parsing, RLE decoding, streaming parser |
| `renderer.py` | Converts display sets to PNG files |
| `ffmpeg.py` | ffprobe track discovery, streaming and batch extraction |
| `interactive.py` | Interactive track/count prompts |
| `shellmenu.py` | Windows Explorer context menu install/uninstall via registry |
| `constants.py` | PQ constants, segment type codes, file extensions |

## Key technical decisions

### YCbCr is limited range
BD (both standard and UHD) uses **limited-range** YCbCr throughout:
- Y: 16–235 (219 levels)  →  `(Y - 16) / 219`
- Cb/Cr: 16–240 (224 levels, centred at 128)  →  `(C - 128) / 224`

This was a source of a past bug (full-range assumed). Do not change to full-range.

### SDR pipeline includes BT.1886 linearization
Blu-ray SDR content is mastered for BT.1886 (gamma 2.4). PC monitors use sRGB (≈ gamma 2.2). The SDR decoder therefore linearises with gamma 2.4 before re-encoding with sRGB gamma so the PNG looks correct on a PC display. It is **not** a direct BT.709 → sRGB passthrough.

### HDR tonemap reference white is 203 nits
The UHD BD reference white is 203 nits. Linear light is normalised by dividing by `203/10000 = 0.0203` before clipping or Reinhard tonemapping. This maps reference-white subtitles to sRGB 1.0.

### Preview samples from the middle of the file
The interactive default of 10 subtitles extracts from the **middle** of the file (seek to `duration/2 - 60s`), not from the start. This gives representative movie content rather than intros or credits. The same cached extraction is reused for the track listing's subtitle count.

### Subtitle counts: exact vs estimated
- **MKV**: `NUMBER_OF_FRAMES` tag provides an exact display-set count for free.
- **MP4 / other**: `nb_frames` stream field used if available.
- **Remaining**: a 2-minute mid-file streaming window is extracted; content display sets are counted and extrapolated to the full duration. Shown as `(~N subtitles est.)`.

### Two extraction strategies

| Situation | Strategy |
|---|---|
| `--first N` or interactive limit | Streaming via pipe — FFmpeg killed once N display sets collected. No temp files. |
| `--tracks all` (no limit) | Batch — single FFmpeg pass extracts all selected tracks to temp `.sup` files, then decoded sequentially. |

### SDR/HDR auto-detection via PQ plausibility analysis
`--mode auto` (default) detects whether each PGS track was mastered for SDR or HDR by analyzing raw palette entries. Detection is **per-track** — subtitle tracks in the same container may originate from different sources (SDR BD vs UHD BD remuxed together). Video stream color metadata is intentionally not used for this reason. Per the UHD BD spec (section 3.9), SDR subtitles are always BT.709 Y'CbCr regardless of the video stream's color primaries, while HDR subtitles are BT.2020 ST 2084 Y'CbCr with 8-bit values multiplied by 4 for 10-bit compositing.

**Primary signal: PQ plausibility test.** Each bright palette entry (Y > 50) is decoded YCbCr → R'G'B' using the BT.2020 matrix. The **95th percentile** of unique PQ channel values is used as the representative metric — this requires at least 5% of distinct palette entries to exceed the threshold before triggering the SDR signal, making it robust against outlier glow/gradient/saturated entries that can appear in HDR content (e.g., animated title sequences with complex graphics). If this representative value exceeds the PQ code value for 1000 nits (~0.75), the PQ interpretation implies unrealistic luminance for the bulk of entries, meaning the content is gamma-encoded SDR. If it stays below 0.65 (~400 nits), the PQ interpretation is plausible and the content is HDR. This test is particularly effective for colored text (gold, yellow, cyan) where Y-only thresholds are ambiguous — e.g., SDR gold text at Y=178 gives R'≈0.92 under BT.2020, corresponding to ~4800 nits in PQ (obviously SDR), while genuine HDR text at 203 nits gives R'≈0.58 (~200 nits).

**Secondary signals:** Y-value thresholds (≥210 SDR, ≤170 HDR) and achromatic entry analysis (Cb/Cr near 128) handle cases where the PQ test is inconclusive.

### Windows Explorer context menu
`--install` registers a cascading context menu under `HKCU\Software\Classes` (no admin required). A shared submenu at `ProofPGS.SubMenu` defines the mode entries; per-extension verbs under `SystemFileAssociations\<ext>\shell\ProofPGS` reference it via `ExtendedSubCommandsKey`. `SystemFileAssociations` is used instead of direct `.ext\shell` so the menu appears regardless of which program owns the file type. The commands embed both the Python interpreter path and the project root directory (via `cd /d`) captured at install time, since the module is not pip-installed. Running `--install` again is idempotent and updates the paths.

### Windows pipe deadlock workaround
`extract_track_streaming()` sends FFmpeg's stderr to `/dev/null` (on Windows, `subprocess.DEVNULL`). If stderr is inherited and FFmpeg writes enough warnings to fill the 4 KB pipe buffer, it blocks — which stalls stdout while we're blocked reading stdout. Do not remove this.

## Adding a new container format

Add the extension to `CONTAINER_EXTENSIONS` in `constants.py`. The rest of the pipeline uses ffprobe/ffmpeg generically and will handle it automatically.

## Colour pipeline reference

**HDR:**
```
BT.2020 YCbCr (limited range)  →  BT.2020 matrix  →  PQ EOTF (linear, 0..1 = 0..10,000 nits)
  →  BT.2020 → BT.709 primary matrix  →  normalise to 203 nits ref white  →  tonemap
  →  sRGB gamma  →  uint8 PNG
```

**SDR:**
```
BT.709 YCbCr (limited range)  →  BT.709 matrix  →  BT.1886 linearise (γ 2.4)
  →  sRGB gamma  →  uint8 PNG
```

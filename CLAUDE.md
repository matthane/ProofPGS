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
- `libpgs` — bundled binary in `proofpgs/bin/` (or on PATH). Handles all PGS file I/O (`.sup`, MKV, M2TS). See [github.com/matthane/libpgs](https://github.com/matthane/libpgs).
- FFmpeg / ffprobe on PATH (optional — only needed for the video stream dynamic range mismatch badge via `probe_video_range()`)

There is no test suite. Validation is done by visual inspection of the output PNGs.

## Module responsibilities

| File | Role |
|---|---|
| `cli.py` | Argument parsing, `main()` entry point |
| `pipeline.py` | High-level orchestration — ties everything together |
| `color.py` | All colour-space math and palette LUT construction |
| `detect.py` | SDR/HDR auto-detection via PQ plausibility analysis |
| `parser.py` | `ds_has_content()` — checks if a display set has renderable content |
| `renderer.py` | Multi-threaded display set rendering and PNG output |
| `libpgs.py` | Adapter for the libpgs CLI — subprocess streaming, track discovery, display-set conversion |
| `ffmpeg.py` | ffprobe video range detection (`probe_video_range()`), track folder naming |
| `interactive.py` | Interactive track/count prompts |
| `shellmenu.py` | Windows Explorer context menu install/uninstall via registry |
| `constants.py` | PQ constants, file extensions, analysis budget (`Budget` class) |
| `style.py` | Terminal styling helpers (colours, badges, cursor control) |
| `assets/` | Bundled resources (fonts, icons) — accessed via `Path(__file__).resolve().parent / "assets"` |
| `bin/` | Bundled libpgs binary (platform-specific, gitignored) |

## Bundled assets and licenses

`proofpgs/assets/` contains resources shipped with the project (fonts, icons). Third-party license files live in `LICENSES/` at the project root.

| Asset | License | File |
|---|---|---|
| Sora Medium (`assets/Sora-Medium.ttf`) | SIL Open Font License 1.1 | `LICENSES/OFL.txt` |
| Sora Regular (`assets/Sora-Regular.ttf`) | SIL Open Font License 1.1 | `LICENSES/OFL.txt` |

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

### Track analysis budget (10s wallclock)
When listing PGS tracks in a container, the analysis phase (SDR/HDR detection) runs under a **10-second wallclock budget**. A single `libpgs stream` pass extracts display sets from all tracks simultaneously via NDJSON streaming. Two mechanisms can terminate libpgs early: a deadline watchdog kills it when the budget expires, and a content-based watchdog kills it as soon as all tracks have conclusive SDR/HDR detection. Tracks that received enough data are fully analyzed; tracks with too few display sets (sparse subtitles) are marked `analysis_bailed = True` and shown as `[not analyzed — too few samples]` in the listing. The user can press `[v]` to re-analyze bailed tracks without a time limit. `--mode validate` runs without a budget and shows scan progress. `--mode validate-fast` runs under the normal budget but prompts to re-analyze sparse tracks afterward.

The budget is controlled by `LISTING_BUDGET_S` in `constants.py` (default 10s). The per-track display-set cap is `ANALYSIS_MAX_DS` (default 125).

### Extraction starts from the beginning (unless `--start`/`--end`)
By default, both analysis and streaming extraction read from the start of the file. The interactive count prompt defaults to up to 10 cached analysis samples (`DEFAULT_INTERACTIVE_COUNT` in `constants.py`), with no additional extraction needed. The user can request a custom count or all subtitles.

When `--start` and/or `--end` are specified, extraction uses libpgs's targeted seeking to jump directly to the requested time range. Analysis still runs from the beginning — SDR/HDR detection is a track-level property, not range-dependent. Because the analysis cache contains display sets from the beginning of the file (not the target range), cache reuse is bypassed when a time range is active: the interactive "cached" default is converted to `DEFAULT_INTERACTIVE_COUNT` with fresh streaming from the target range, and the cache partition in `_batch_extract_with_limit()` forces all tracks to stream. The `discover_tracks(keep_alive=True)` process reuse optimization is also disabled when `--start` is set, since the discovery process starts at byte 0 and can't be reused for targeted extraction.

### Extraction via libpgs

All file I/O goes through the bundled `libpgs` binary, which streams PGS data as NDJSON over a subprocess pipe. No temp files are created — display sets are streamed into memory. libpgs handles RLE decoding and object fragment reassembly, outputting pre-decoded bitmap data (base64-encoded palette indices). The libpgs adapter (`libpgs.py`) handles subprocess management, NDJSON parsing, and conversion to the internal display-set format.

| Situation | Strategy |
|---|---|
| Interactive default (cached) | Reuses display sets already collected during analysis, capped at `DEFAULT_INTERACTIVE_COUNT` (10). No additional extraction. |
| `--first N` or custom interactive count (single track) | `libpgs stream <file> -t <id>` — pipe closed once N display sets collected. Reuses analysis cache if it already has enough content. |
| `--first N` or custom interactive count (multi-track) | Single `libpgs stream <file> -t id1,id2,...` pass with reader-side per-track limiting. A reader thread demuxes into per-track queues and sends a sentinel once each track hits the limit; concurrent renderer threads consume the queues. Tracks with enough cached analysis data are rendered from cache without streaming. |
| `--tracks all` (no limit), multiple tracks | Single `libpgs stream <file> -t id1,id2,...` pass. A reader thread demuxes into per-track queues consumed by concurrent renderer threads. Avoids redundant MKV header / cues parsing and (for containers without cues) re-reading the file from the start. |
| `--start`/`--end` (any of the above) | Appends `--start`/`--end` to the libpgs command. libpgs seeks directly to the estimated byte offset — data before the start point is not read. Composes with `--first N` (first N content display sets within the range). Analysis is unaffected (always from byte 0). |

### SDR/HDR auto-detection via PQ plausibility analysis
`--mode auto` (default) detects whether each PGS track was mastered for SDR or HDR by analyzing raw palette entries. Detection and mode resolution are **per-track** — each track is decoded with its own detected color pipeline independently. A container with mixed SDR and HDR tracks (e.g. SDR BD and UHD BD remuxed together) processes each track correctly without falling back to compare mode. Only tracks where detection is genuinely inconclusive fall back to compare. Video stream color metadata is intentionally not used because subtitle tracks may originate from different sources. Per the UHD BD spec (section 3.9), SDR subtitles are always BT.709 Y'CbCr regardless of the video stream's color primaries, while HDR subtitles are BT.2020 ST 2084 Y'CbCr with 8-bit values multiplied by 4 for 10-bit compositing.

Output filenames include the decoded range as a suffix: `ds_0001_1234ms_sdr.png`, `ds_0001_1234ms_hdr.png`, or `ds_0001_1234ms_compare.png`.

**Fade-in / ghost entry filtering.** Before analyzing palette entries, detection collects the set of palette entry IDs actually referenced by each object's bitmap (`set(obj["bitmap"])` — each byte is a palette index). Only palette entries actually referenced by the bitmap are considered. This is critical because PGS fade-in frames define the full palette — including the bright text colour at high alpha — but only reference dim, low-alpha entries in the actual bitmap. Without this filter, a single unreferenced "ghost" palette entry (e.g. Y=200, A=192) could dominate the PQ metric and trigger a false SDR verdict on HDR content. Additionally, entries with alpha < 32 are excluded (`_MIN_ALPHA` in `detect.py`), filtering out anti-aliasing fringe and fade-in ramp entries that are functionally invisible.

**Primary signal: PQ plausibility test.** Each visible, bitmap-referenced palette entry (Y > 50, alpha >= 32) is decoded YCbCr → R'G'B' using the BT.2020 matrix. The **95th percentile** of unique PQ channel values is used as the representative metric — this requires at least 5% of distinct palette entries to exceed the threshold before triggering the SDR signal, making it robust against outlier glow/gradient/saturated entries that can appear in HDR content (e.g., animated title sequences with complex graphics). For small sample sizes (fewer than 20 unique values), the percentile index is clamped to always exclude at least the top unique value, preventing a single outlier from dominating the metric. If this representative value exceeds the PQ code value for 1000 nits (~0.75), the PQ interpretation implies unrealistic luminance for the bulk of entries, meaning the content is gamma-encoded SDR. If it stays below 0.65 (~400 nits), the PQ interpretation is plausible and the content is HDR. This test is particularly effective for colored text (gold, yellow, cyan) where Y-only thresholds are ambiguous — e.g., SDR gold text at Y=178 gives R'≈0.92 under BT.2020, corresponding to ~4800 nits in PQ (obviously SDR), while genuine HDR text at 203 nits gives R'≈0.58 (~200 nits).

**Secondary signals:** Y-value thresholds (≥210 SDR, ≤170 HDR) and achromatic entry analysis (Cb/Cr near 128) handle cases where the PQ test is inconclusive.

### Dynamic range mismatch badge
For container inputs, `probe_video_range()` in `ffmpeg.py` runs a separate ffprobe query on video streams to detect the video's dynamic range. This is the only remaining use of ffprobe — if ffprobe is not on PATH, the mismatch badge is silently skipped. It checks `color_transfer` first (`smpte2084`/`arib-std-b67` → HDR, `bt709`/`smpte170m`/etc. → SDR), falls back to `color_primaries` (`bt2020` → HDR), then checks `side_data_list` for a Dolby Vision configuration record (DV Profile 5 and others may lack standard color metadata entirely), then defaults to SDR — because SDR Blu-ray rips almost never carry explicit color metadata, while HDR standards require signaling. Attached pictures (cover art) are skipped. The track listing shows a `Video stream: HDR/SDR` header and appends a `Dynamic range mismatch` badge (amber) to any subtitle track whose palette-based detection verdict differs from the video stream's range. This is **informational only** — it does not affect subtitle processing or detection. Video stream metadata is still intentionally not used for detection itself.

### Windows Explorer context menu
`--install` registers a cascading context menu under `HKCU\Software\Classes` (no admin required). Two shared submenus define the mode entries: `ProofPGS.SupMenu` for `.sup` files and `ProofPGS.ContainerMenu` for container formats (`.mkv`, `.mk3d`, `.m2ts`). `.sup` files get a simple "Validate" entry (direct parsing, no FFmpeg budget), while containers get both "Validate (may be slow)" and "Validate fast (skips sparse tracks)". Per-extension verbs under `SystemFileAssociations\<ext>\shell\ProofPGS` reference the appropriate submenu via `ExtendedSubCommandsKey`. `SystemFileAssociations` is used instead of direct `.ext\shell` so the menu appears regardless of which program owns the file type. The commands embed both the Python interpreter path and the project root directory (via `cd /d`) captured at install time, since the module is not pip-installed. Running `--install` again is idempotent and updates the paths.

### Subprocess pipe deadlock workaround
The libpgs adapter sends the subprocess's stderr to `subprocess.DEVNULL`. If stderr is inherited and the child process writes enough output to fill the 4 KB pipe buffer, it blocks — which stalls stdout while we're blocked reading stdout. Do not remove this.

## Adding a new container format

Add the extension to `CONTAINER_EXTENSIONS` in `constants.py`. Only formats that can carry PGS subtitle streams should be added (currently MKV, MK3D, and M2TS). PGS is an HDMV/Blu-ray spec — only Matroska (`.mkv`/`.mk3d`) and BDAV transport streams (`.m2ts`) properly support it. `.mk3d` is identical to MKV but indicates 3D content. Generic `.ts`, MP4, AVI, and WMV cannot carry PGS. The container format must also be supported by libpgs — check [libpgs](https://github.com/matthane/libpgs) for supported formats.

## Release workflow

`.github/workflows/release.yml` runs when a GitHub Release is published. It builds platform-specific archives (Windows x64, Linux x64, macOS x64, macOS ARM64) that bundle ProofPGS with a libpgs binary compiled from source.

**How it works:**
1. Resolves the latest tagged release from `matthane/libpgs`
2. Checks out the libpgs source at that tag and builds it with `cargo build --release` on each platform's native runner
3. Copies the compiled binary into `proofpgs/bin/`
4. Reads the version from `__init__.py` and stages a clean release directory (only `proofpgs/`, `LICENSE.txt`, `README.md`, `LICENSES/`, and `BUILD_INFO.txt` — no `.git`, `CLAUDE.md`, `dev/`, etc.)
5. Generates a Sigstore artifact attestation via `actions/attest-build-provenance@v2` for each archive
6. Uploads the archives to the GitHub Release

**BUILD_INFO.txt** is included in every release archive with provenance metadata: the libpgs tag and commit hash, the ProofPGS commit SHA, the build target, and a link to the workflow run.

**Artifact attestations** cryptographically link each release archive to the workflow run and source commit that produced it. Users can verify with `gh attestation verify <file> --repo matthane/ProofPGS`.

**To create a release:** Tag a commit (e.g. `v1.2.1`), create a GitHub Release from that tag, and publish it. The workflow handles everything automatically.

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

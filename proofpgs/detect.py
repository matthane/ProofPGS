"""Color space detection for PGS subtitle streams (SDR vs HDR).

Analyzes raw YCbCr palette entries to determine whether a PGS stream was
mastered for SDR (BT.709 + BT.1886) or HDR (BT.2020 + PQ).

Primary signal: **PQ plausibility test**.  For each bright palette entry,
decode YCbCr → R'G'B' using the BT.2020 matrix and collect the max
channel value.  Rather than using the single highest value (which can
be skewed by outlier glow/gradient/saturated entries in HDR content),
we use the **95th percentile** of unique PQ channel values for the
implausibility/plausibility checks.  This requires at least 5% of
distinct palette entries to agree before triggering the SDR signal —
robust against any number of outliers while still catching SDR content
where the vast majority of entries have implausible PQ values.

If the representative PQ value exceeds the code value for ~1000 nits
(~0.75), the PQ interpretation implies luminances far too high for
subtitle content → gamma-encoded SDR, not PQ-encoded HDR.

This test is particularly effective for colored text (gold, yellow, cyan)
where Y-value-only thresholds are ambiguous.  For example, SDR gold text
at Y=178 decodes to R'≈0.92 under BT.2020, giving ~4800 nits in PQ —
obviously SDR.  Genuine HDR text at 203 nits gives R'≈0.58 → ~200 nits.

Secondary signals: Y-value thresholds and achromatic entry analysis
handle cases where the PQ test is inconclusive.
"""

from .constants import SEG_PDS, SEG_ODS
from .parser import parse_pds, parse_ods, rle_used_entries

# --- BT.2020 YCbCr → R'G'B' matrix coefficients (limited-range normalised) ---
# R' = Yn + 1.4746 * Crn
# G' = Yn - 0.1645 * Cbn - 0.5713 * Crn
# B' = Yn + 1.8814 * Cbn
_BT2020_CR_R = 1.4746
_BT2020_CB_G = -0.1645
_BT2020_CR_G = -0.5713
_BT2020_CB_B = 1.8814

# PQ code value thresholds (0–1 normalised, not limited-range Y).
# Computed via PQ OETF:  1000 nits → 0.7518,  400 nits → 0.6375.
#
# _PQ_IMPLAUSIBLE: if the representative PQ channel value (p95 of unique
#   entries) exceeds this after BT.2020 decode, the PQ interpretation
#   gives >1000 nits for the bulk of entries — far too bright for
#   subtitle content.  Almost certainly SDR.
# _PQ_PLAUSIBLE:   if the representative value stays below this, the PQ
#   interpretation gives <400 nits — perfectly reasonable for HDR
#   subtitle content at or near reference white (203 nits).
# _PQ_PERCENTILE: use this percentile of unique PQ channel values as the
#   representative value.  At 0.95, at least 5% of distinct palette
#   entries must exceed the threshold to trigger the SDR signal.  This is
#   robust against outlier glow/gradient/saturated entries in HDR content
#   while still catching SDR content where most entries are implausible.
_PQ_IMPLAUSIBLE = 0.75   # ~1000 nits
_PQ_PLAUSIBLE = 0.65     # ~400 nits
_PQ_PERCENTILE = 0.95    # 95th percentile of unique PQ values

# Y-value thresholds (limited-range Y, 8-bit) — secondary signal.
_Y_SDR_HIGH = 210   # SDR yellow = 219, SDR white = 235
_Y_HDR_HIGH = 170   # HDR 1000 nits = 181, HDR ref white = 143
_Y_SDR_MED = 195    # medium-confidence boundary in ambiguous zone

# Achromatic tolerance: entries with Cb and Cr both within this distance
# of 128 are treated as white/gray/black (color-matrix-independent).
_ACHRO_TOL = 3

# Minimum Y to consider an entry meaningful (skip shadows/outlines).
_MIN_Y = 50

# Minimum alpha to consider a palette entry visible.  PGS fade-in frames
# define the full palette but with near-zero alpha (1–11) on most entries.
# These are functionally invisible and should not influence detection.
# 32 (~12.5% opacity) excludes anti-aliasing fringe and fade-in entries
# while keeping any genuinely rendered content.
_MIN_ALPHA = 32


def _bt2020_max_channel(y, cr, cb):
    """Decode limited-range YCbCr to BT.2020 R'G'B', return max channel.

    Returns the maximum of (R', G', B') after BT.2020 matrix decode.
    This value represents a PQ code if the content is HDR, or a
    gamma-encoded value if SDR.  In either case, values above ~0.75
    imply >1000 nit luminance under PQ — implausible for subtitles.
    """
    yn = (y - 16.0) / 219.0
    crn = (cr - 128.0) / 224.0
    cbn = (cb - 128.0) / 224.0
    rp = yn + _BT2020_CR_R * crn
    gp = yn + _BT2020_CB_G * cbn + _BT2020_CR_G * crn
    bp = yn + _BT2020_CB_B * cbn
    return max(rp, gp, bp)


def detect_from_palettes(display_sets: list) -> dict:
    """Analyze palette entries from parsed display sets.

    Iterates every PDS segment, collects Y values and PQ-plausibility
    metrics from opaque entries (alpha > 0), and applies a multi-signal
    detection algorithm.

    Args:
        display_sets: list of display sets (each a list of segment dicts
                      with keys ``type``, ``pts``, ``payload``).

    Returns:
        dict with keys:
          verdict:          "hdr" | "sdr" | None
          confidence:       "high" | "medium" | "low"
          max_y:            int  (highest Y among opaque entries, or 0)
          max_achromatic_y: int | None  (highest Y among achromatic entries)
          max_pq_channel:   float  (representative PQ channel value — 95th
                                   percentile of unique entries)
          num_palettes:     int  (number of PDS segments analyzed)
    """
    max_y = 0
    max_achromatic_y = None
    pq_values = []  # all PQ channel values from bright entries
    num_palettes = 0
    has_bright = False  # at least one entry with Y > _MIN_Y

    for ds in display_sets:
        # Collect palette entry IDs actually referenced by the bitmap.
        # Fade-in frames define the full palette (including bright text
        # at high alpha) but only reference dim, low-alpha entries in
        # the RLE data.  Considering unreferenced entries would let a
        # single ghost palette entry corrupt the detection verdict.
        used_ids = set()
        for seg in ds:
            if seg["type"] == SEG_ODS:
                ods = parse_ods(seg["payload"])
                if ods and ods.get("rle"):
                    used_ids |= rle_used_entries(ods["rle"])

        for seg in ds:
            if seg["type"] != SEG_PDS:
                continue

            palette = parse_pds(seg["payload"])
            if not palette:
                continue
            num_palettes += 1

            for eid, (y, cr, cb, alpha) in palette.items():
                if alpha < _MIN_ALPHA:
                    continue
                # Skip entries not referenced by the bitmap.
                if used_ids and eid not in used_ids:
                    continue
                if y > max_y:
                    max_y = y

                if y > _MIN_Y:
                    has_bright = True
                    # PQ plausibility: decode as BT.2020 and check max channel
                    ch = _bt2020_max_channel(y, cr, cb)
                    pq_values.append(ch)

                # Achromatic check: Cb and Cr both near 128
                if abs(cb - 128) <= _ACHRO_TOL and abs(cr - 128) <= _ACHRO_TOL:
                    if max_achromatic_y is None or y > max_achromatic_y:
                        max_achromatic_y = y

    # Compute representative PQ channel value: use the 95th percentile of
    # unique values.  This requires at least 5% of distinct palette entries
    # to exceed the threshold before triggering the SDR signal — robust
    # against outlier glow/gradient/saturated entries in HDR content.
    #
    # For small samples (< 20 unique values), int(n * 0.95) == n-1, so the
    # raw p95 always selects the maximum — a single outlier (e.g. a fade-in
    # text entry at Y=200) dominates the metric.  Clamping to len-2 ensures
    # we always exclude at least the top unique value, so one outlier can
    # never single-handedly trigger the SDR signal.
    if pq_values:
        unique_pq = sorted(set(pq_values))
        if len(unique_pq) >= 2:
            p95_idx = min(int(len(unique_pq) * _PQ_PERCENTILE),
                          len(unique_pq) - 2)
            max_pq_channel = unique_pq[p95_idx]
        else:
            # Single unique PQ value — use it directly.
            max_pq_channel = unique_pq[0]
    else:
        max_pq_channel = 0.0

    # --- Decision logic ---
    if num_palettes == 0 or not has_bright:
        return _result(None, "low", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 1. PQ implausibility test (strongest SDR signal).
    #    If the 95th-percentile PQ channel value exceeds 0.75, the bulk of
    #    bright entries decode to >1000 nits under PQ — almost certainly SDR.
    #    Using p95 is robust against any number of outlier entries.
    if max_pq_channel > _PQ_IMPLAUSIBLE:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 2. Clear Y-value signals (achromatic preferred, then global max).
    ref_y = max_achromatic_y if max_achromatic_y is not None and max_achromatic_y > _MIN_Y else None

    if ref_y is not None and ref_y >= _Y_SDR_HIGH:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    if max_y >= _Y_SDR_HIGH:
        return _result("sdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    if ref_y is not None and ref_y <= _Y_HDR_HIGH:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    if max_y <= _Y_HDR_HIGH:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 3. PQ plausibility test (strong HDR signal).
    #    If all channels stay below 0.65, the PQ interpretation gives
    #    <400 nits — perfectly reasonable for HDR subtitles.
    if max_pq_channel <= _PQ_PLAUSIBLE:
        return _result("hdr", "high", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)

    # 4. Ambiguous zone (170 < max_y < 210, 0.65 < max_pq < 0.75).
    #    Fall back to medium-confidence Y threshold.
    if max_y >= _Y_SDR_MED:
        return _result("sdr", "medium", max_y, max_achromatic_y,
                       max_pq_channel, num_palettes)
    return _result("hdr", "medium", max_y, max_achromatic_y,
                   max_pq_channel, num_palettes)


def _result(verdict, confidence, max_y, max_achromatic_y,
            max_pq_channel, num_palettes):
    """Build a detection result dict."""
    return {
        "verdict": verdict,
        "confidence": confidence,
        "max_y": max_y,
        "max_achromatic_y": max_achromatic_y,
        "max_pq_channel": round(max_pq_channel, 3),
        "num_palettes": num_palettes,
    }


def format_detection(result: dict) -> str:
    """Format a detection result for terminal output.

    Returns a human-readable string like:
        "HDR (Y-max: 143, PQ-max: 0.586, 24 palettes)"
        "SDR (Y-max: 178, PQ-max: 0.924, 48 palettes)"
    """
    v = result["verdict"]
    if v is None:
        return "unknown (insufficient palette data)"

    label = v.upper()
    parts = [f"Y-max: {result['max_y']}"]
    if result.get("max_pq_channel"):
        parts.append(f"PQ-max: {result['max_pq_channel']:.3f}")
    if result["max_achromatic_y"] is not None:
        parts.append(f"achromatic Y-max: {result['max_achromatic_y']}")
    parts.append(f"{result['num_palettes']} palette(s)")
    if result["confidence"] != "high":
        parts.append(f"{result['confidence']} confidence")

    return f"{label} ({', '.join(parts)})"

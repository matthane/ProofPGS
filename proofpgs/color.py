"""Colour-space math: PQ EOTF, gamut mapping, palette decoding (HDR & SDR)."""

import numpy as np

from .constants import PQ_M1, PQ_M2, PQ_C1, PQ_C2, PQ_C3


def pq_eotf(E: np.ndarray) -> np.ndarray:
    """ST 2084 (PQ) EOTF.
    Input:  PQ code values in [0, 1]
    Output: Absolute linear light, normalised to 10,000 nits (so 1.0 = 10,000 nits)
    """
    E  = np.clip(E, 0.0, 1.0)
    t  = np.power(E, 1.0 / PQ_M2)
    num = np.maximum(t - PQ_C1, 0.0)
    den = PQ_C2 - PQ_C3 * t
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    return np.power(num / den, 1.0 / PQ_M1)


# BT.2020 -> BT.709 colour primaries matrix (linear light, both normalised)
# Derived from respective XYZ matrices. Operates on linear RGB.
BT2020_TO_BT709 = np.array([
    [ 1.6604910,  -0.5876411, -0.0728499],
    [-0.1245505,   1.1328999, -0.0083494],
    [-0.0181508,  -0.1005789,  1.1187297],
], dtype=np.float64)


def srgb_gamma(linear: np.ndarray) -> np.ndarray:
    """Linear light [0,1] -> sRGB gamma encoded [0,1]."""
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(np.maximum(linear, 0.0), 1.0 / 2.4) - 0.055
    )


def decode_palette_hdr(entries: dict, tonemap: str) -> np.ndarray:
    """Build a 256x4 RGBA LUT from HDR (BT.2020+PQ) palette entries.

    Pipeline:
      YCbCr (BT.2020, limited range)
        -> R'G'B' (BT.2020 non-linear, PQ encoded)
        -> linear RGB (BT.2020, absolute nits via PQ EOTF)
        -> linear RGB (BT.709, via primary matrix)
        -> tonemap to SDR [0,1]
        -> sRGB gamma
        -> uint8 RGBA
    """
    if not entries:
        return np.zeros((256, 4), dtype=np.uint8)

    ids  = np.array(list(entries.keys()),   dtype=np.int32)
    vals = np.array(list(entries.values()), dtype=np.float64)

    Y  = vals[:, 0]
    Cr = vals[:, 1]   # Cr before Cb, per PGS spec
    Cb = vals[:, 2]
    A  = vals[:, 3]

    # --- Step 1: BT.2020 YCbCr (limited range) -> R'G'B' (PQ encoded, 0..1) ---
    # BD/UHD BD uses limited-range YCbCr: Y 16-235 (219 levels),
    # Cb/Cr 16-240 (224 levels, centred at 128).
    Yn  = (Y  - 16.0) / 219.0
    Crn = (Cr - 128.0) / 224.0
    Cbn = (Cb - 128.0) / 224.0

    R_pq = np.clip(Yn + 1.4746  * Crn,                          0.0, 1.0)
    G_pq = np.clip(Yn - 0.1645  * Cbn - 0.5713 * Crn,           0.0, 1.0)
    B_pq = np.clip(Yn + 1.8814  * Cbn,                          0.0, 1.0)

    # --- Step 2: PQ EOTF -> linear light (0..1, where 1.0 = 10,000 nits) ---
    R_lin = pq_eotf(R_pq)
    G_lin = pq_eotf(G_pq)
    B_lin = pq_eotf(B_pq)

    # --- Step 3: BT.2020 -> BT.709 primary conversion ---
    # Stack into (N, 3) for matrix multiply
    rgb_2020 = np.stack([R_lin, G_lin, B_lin], axis=1)  # (N, 3)
    rgb_709  = rgb_2020 @ BT2020_TO_BT709.T              # (N, 3)

    R_709 = rgb_709[:, 0]
    G_709 = rgb_709[:, 1]
    B_709 = rgb_709[:, 2]

    # --- Step 4: Tonemap HDR -> SDR ---
    # Normalise so that the UHD BD reference white (203 nits) maps to SDR 1.0.
    # 203 nits / 10,000 nits = 0.0203
    REF_WHITE = 203.0 / 10000.0
    R_709 /= REF_WHITE
    G_709 /= REF_WHITE
    B_709 /= REF_WHITE

    if tonemap == "reinhard":
        # Per-channel Reinhard on the BT.709 signal
        R_709 = np.maximum(R_709, 0.0)
        G_709 = np.maximum(G_709, 0.0)
        B_709 = np.maximum(B_709, 0.0)
        R_709 = R_709 / (1.0 + R_709)
        G_709 = G_709 / (1.0 + G_709)
        B_709 = B_709 / (1.0 + B_709)
    else:
        # Hard clip — preserves colour accuracy for values under 203 nits,
        # clips anything brighter. Good for subtitles that sit at or below
        # reference white.
        R_709 = np.clip(R_709, 0.0, 1.0)
        G_709 = np.clip(G_709, 0.0, 1.0)
        B_709 = np.clip(B_709, 0.0, 1.0)

    # --- Step 5: sRGB gamma encoding ---
    R_out = np.clip(np.round(srgb_gamma(R_709) * 255.0), 0, 255).astype(np.uint8)
    G_out = np.clip(np.round(srgb_gamma(G_709) * 255.0), 0, 255).astype(np.uint8)
    B_out = np.clip(np.round(srgb_gamma(B_709) * 255.0), 0, 255).astype(np.uint8)

    lut = np.zeros((256, 4), dtype=np.uint8)
    mask = (ids >= 0) & (ids <= 255)
    valid = ids[mask]
    lut[valid, 0] = R_out[mask]
    lut[valid, 1] = G_out[mask]
    lut[valid, 2] = B_out[mask]
    lut[valid, 3] = A[mask].astype(np.uint8)
    return lut


def decode_palette_sdr(entries: dict) -> np.ndarray:
    """Build a 256x4 RGBA LUT from SDR (BT.709) palette entries.

    Pipeline:
      YCbCr (BT.709, limited range)
        -> R'G'B' (BT.709 gamma)
        -> linear light (BT.1886 EOTF, gamma 2.4)
        -> sRGB gamma encoding
        -> uint8 RGBA
    """
    if not entries:
        return np.zeros((256, 4), dtype=np.uint8)

    ids  = np.array(list(entries.keys()),   dtype=np.int32)
    vals = np.array(list(entries.values()), dtype=np.float64)

    Y  = vals[:, 0]
    Cr = vals[:, 1]
    Cb = vals[:, 2]
    A  = vals[:, 3]

    # BT.709 limited-range YCbCr -> R'G'B' (already gamma-encoded)
    # BD uses limited-range: Y 16-235 (219 levels), Cb/Cr 16-240 (224 levels).
    Yn  = (Y  - 16.0) / 219.0
    Crn = (Cr - 128.0) / 224.0
    Cbn = (Cb - 128.0) / 224.0

    R = np.clip(Yn + 1.5748  * Crn,                        0.0, 1.0)
    G = np.clip(Yn - 0.1873  * Cbn - 0.4681 * Crn,         0.0, 1.0)
    B = np.clip(Yn + 1.8556  * Cbn,                        0.0, 1.0)

    # BD SDR is designed for BT.1886 EOTF (gamma 2.4).  PC monitors use sRGB
    # EOTF (~gamma 2.2).  Linearise with BT.1886 then re-encode as sRGB so the
    # PNG looks correct on a PC display.
    R_lin = np.power(R, 2.4)
    G_lin = np.power(G, 2.4)
    B_lin = np.power(B, 2.4)

    R_out = np.clip(np.round(srgb_gamma(R_lin) * 255.0), 0, 255).astype(np.uint8)
    G_out = np.clip(np.round(srgb_gamma(G_lin) * 255.0), 0, 255).astype(np.uint8)
    B_out = np.clip(np.round(srgb_gamma(B_lin) * 255.0), 0, 255).astype(np.uint8)

    lut = np.zeros((256, 4), dtype=np.uint8)
    mask = (ids >= 0) & (ids <= 255)
    valid = ids[mask]
    lut[valid, 0] = R_out[mask]
    lut[valid, 1] = G_out[mask]
    lut[valid, 2] = B_out[mask]
    lut[valid, 3] = A[mask].astype(np.uint8)
    return lut

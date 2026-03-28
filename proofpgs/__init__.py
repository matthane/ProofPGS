"""ProofPGS — PGS subtitle decoder with HDR (UHD BD) and SDR (BD) support."""

__version__ = "1.4.1"

from .parser import ds_has_content
from .renderer import render_ds, crop_to_content
from .color import decode_palette_hdr, decode_palette_sdr
from .pipeline import process_sup_file, process_container
from .cli import main

__all__ = [
    "ds_has_content",
    "render_ds",
    "crop_to_content",
    "decode_palette_hdr",
    "decode_palette_sdr",
    "process_sup_file",
    "process_container",
    "main",
]

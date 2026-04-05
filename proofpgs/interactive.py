"""Interactive prompts for track and count selection."""

import sys

from .constants import DEFAULT_INTERACTIVE_COUNT
from .style import bold, dim


def select_tracks_interactive(tracks: list,
                              has_bailed: bool = False) -> list | str:
    """Prompt the user to select which PGS tracks to process.

    Returns a list of track indices, or the string ``"validate"`` when
    the user chooses to re-analyze bailed tracks.
    """
    default_indices = (
        [i for i, t in enumerate(tracks) if not t.get("analysis_bailed")]
        if has_bailed else list(range(len(tracks)))
    )
    default_label = "All validated tracks" if has_bailed else "All tracks"

    print("Which tracks do you want to process?")
    print(f"  {bold('[Enter]')}    {default_label}")
    print(f"  {bold('[numbers]')}  Specific tracks, e.g. 1,3,4")
    if has_bailed:
        print(f"  {bold('[v]')}        Validate unanalyzed tracks first (may take longer)")
    try:
        choice = input("> ").strip()
    except EOFError:
        print()
        return list(range(len(tracks)))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return default_indices

    if has_bailed and choice.lower() == "v":
        return "validate"

    try:
        # User input is 1-based; convert to 0-based internal indices.
        indices = [int(x.strip()) - 1 for x in choice.split(",")]
        valid = [i for i in indices if 0 <= i < len(tracks)]
        if not valid:
            print(dim(f"  No valid track numbers entered. Processing {default_label.lower()}."))
            return default_indices
        return valid
    except ValueError:
        print(dim(f"  Invalid input. Processing {default_label.lower()}."))
        return default_indices


def confirm_validate_bailed() -> bool:
    """Ask whether to fully validate bailed (unanalyzed) tracks."""
    print("Some tracks could not be analyzed within the time limit.")
    print(f"  {bold('[v]')}      Validate remaining tracks (may take longer)")
    print(f"  {bold('[Enter]')}  Skip and exit")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        print()
        return False
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    return choice == "v"


def select_count_interactive(has_cues: bool = False) -> int | None | str:
    """Prompt the user for how many subtitles to process per track.

    Returns ``"cached"`` (use analysis cache only), a positive int, or
    ``None`` (process all subtitles in the selected tracks).
    """
    all_label = "All" if has_cues else "All (reads entire file)"
    cached_label = "1 (cached)" if has_cues else f"Up to {DEFAULT_INTERACTIVE_COUNT} (cached)"
    print("How many subtitles to process per track?")
    print(f"  {bold('[Enter]')}    {cached_label}")
    print(f"  {bold('[number]')}   Custom count")
    print(f"  {bold('[a]')}        {all_label}")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        print()
        return "cached"
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return "cached"
    if choice in ("a", "all"):
        return None
    try:
        n = int(choice)
        return n if n > 0 else "cached"
    except ValueError:
        print(dim("  Invalid input. Using cached subtitles."))
        return "cached"


def select_count_interactive_sup(total: int) -> int | None:
    """Prompt for how many subtitles to export (.sup file).

    For .sup files the entire file is already parsed into memory, so
    'all' is the natural default (no additional I/O needed).

    Returns a positive int (custom count) or ``None`` (export all).
    """
    print(f"How many subtitles to export? ({total} available)")
    print(f"  {bold('[Enter]')}    All {total}")
    print(f"  {bold('[number]')}   Custom count")
    try:
        choice = input("> ").strip()
    except EOFError:
        print()
        return None
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return None
    try:
        n = int(choice)
        return n if n > 0 else None
    except ValueError:
        print(dim("  Invalid input. Exporting all."))
        return None

"""Interactive prompts for track and count selection."""

import sys

from .style import bold, dim


def select_tracks_interactive(tracks: list,
                              has_bailed: bool = False) -> list | str:
    """Prompt the user to select which PGS tracks to process.

    Returns a list of track indices, or the string ``"validate"`` when
    the user chooses to re-analyze bailed tracks.
    """
    print("Which tracks do you want to process?")
    print(f"  {bold('[Enter]')}    All tracks")
    print(f"  {bold('[numbers]')}  Specific tracks, e.g. 0,2,3")
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
        return list(range(len(tracks)))

    if has_bailed and choice.lower() == "v":
        return "validate"

    try:
        indices = [int(x.strip()) for x in choice.split(",")]
        valid = [i for i in indices if 0 <= i < len(tracks)]
        if not valid:
            print(dim("  No valid track numbers entered. Processing all tracks."))
            return list(range(len(tracks)))
        return valid
    except ValueError:
        print(dim("  Invalid input. Processing all tracks."))
        return list(range(len(tracks)))


def select_count_interactive() -> int | None:
    """Prompt the user for how many subtitles to process per track."""
    print("How many subtitles to process per track?")
    print(f"  {bold('[Enter]')}    10 (default — fast preview)")
    print(f"  {bold('[number]')}   Custom count")
    print(f"  {bold('[a]')}        All (reads entire file)")
    try:
        choice = input("> ").strip().lower()
    except EOFError:
        print()
        return 10
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    if not choice:
        return 10
    if choice in ("a", "all"):
        return None
    try:
        n = int(choice)
        return n if n > 0 else 10
    except ValueError:
        print(dim("  Invalid input. Using default (10)."))
        return 10

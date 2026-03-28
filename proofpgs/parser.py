"""PGS display-set content helpers."""


def ds_has_content(ds: dict) -> bool:
    """Check if a display set contains renderable subtitle content.

    PGS subtitles use paired display sets: one to show (with object bitmap
    data) and one to clear (composition with no objects).  Only the "show"
    sets produce a visible PNG.
    """
    return bool(ds.get("objects"))

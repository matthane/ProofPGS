"""File manager context menu integration for ProofPGS.

Supports Windows Explorer (registry), Linux desktop file managers
(freedesktop .desktop files), and macOS Finder (Quick Actions via
Automator .workflow bundles).
"""

import shutil
import subprocess
import sys
from pathlib import Path

from .constants import SUP_EXTENSIONS, CONTAINER_EXTENSIONS
from .style import (
    dim,
    box_top, box_bottom, box_row, box_blank,
    status_ok, status_err,
)

_MENU_NAME = "ProofPGS"
_SUBMENU_SUP = "ProofPGS.SupMenu"
_SUBMENU_CONTAINER = "ProofPGS.ContainerMenu"

# (registry_name, display_label, mode_value, use_pause)
# use_pause: validate exits immediately so needs & pause to keep the window open
# Labels use a single '&'.  Windows registry MUIVerb requires '&&' for a
# literal ampersand — _win_label() doubles them at write time.
_COMMON_MODES = [
    ("01_auto",     "Auto export (detect color space)",      "auto",     False),
    ("02_compare",  "Compare (SDR & HDR side-by-side)",      "compare",  False),
    ("03_hdr",      "Export as HDR (BT.2020+PQ)",            "hdr",      False),
    ("04_sdr",      "Export as SDR (BT.709)",                "sdr",      False),
]

# .sup files are parsed directly — no FFmpeg analysis budget, single track
_SUP_MODES = _COMMON_MODES + [
    ("05_validate", "Validate", "validate", True),
]

# Containers need FFmpeg analysis; validate-fast skips sparse tracks
_CONTAINER_MODES = _COMMON_MODES + [
    ("05_validate",      "Validate (show track info only, may be slow)", "validate", True),
    ("06_validate_fast", "Validate fast (skips sparse tracks)",          "validate-fast", False),
]


def _win_label(label: str) -> str:
    """Escape '&' as '&&' for Windows registry MUIVerb values."""
    return label.replace("&", "&&")

# MIME types for Linux .desktop files
_SUP_MIME_TYPES = "application/x-pgs-subtitle"
_CONTAINER_MIME_TYPES = "video/x-matroska;video/mp2t"

# macOS UTIs for Automator workflow file filtering
_CONTAINER_UTIS = ["org.matroska.mkv", "public.mpeg-2-transport-stream"]
_SUP_UTIS = ["public.data"]  # .sup has no registered UTI; filter by extension in script


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _all_extensions():
    """Return sorted list of all supported file extensions."""
    return sorted(SUP_EXTENSIONS | CONTAINER_EXTENSIONS)


def _is_pip_installed() -> bool:
    """Check whether ProofPGS was installed as a pip package."""
    from importlib.metadata import PackageNotFoundError, metadata
    try:
        metadata("proofpgs")
        return True
    except PackageNotFoundError:
        return False


def _print_install_box(title: str, sup_exts, container_exts,
                       post_lines=()) -> None:
    """Render a framed summary of what was just installed.

    The framed portion lists registered extensions and the installed
    modes.  *post_lines* are reference lines (paths, platform notes)
    printed *below* the box — where long file paths can run to their
    natural length without being clipped by the box width.
    """
    print(box_top(title))
    print(box_row(f" Registered for {len(sup_exts) + len(container_exts)} "
                  f"file types: {dim(' '.join(sorted(sup_exts + container_exts)))}"))
    print(box_blank())
    print(box_row(f" Subtitle files ({' '.join(sup_exts)}):"))
    for _, label, _, _ in _SUP_MODES:
        print(box_row(f"   {dim('-')} {label}"))
    print(box_blank())
    print(box_row(f" Container files ({' '.join(container_exts)}):"))
    for _, label, _, _ in _CONTAINER_MODES:
        print(box_row(f"   {dim('-')} {label}"))
    print(box_bottom())
    for line in post_lines:
        print(f"  {dim(line)}")


def _project_root() -> str:
    """Return the project root directory (parent of the proofpgs package)."""
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_install_paths() -> dict:
    """Determine executable paths for command construction.

    Returns a dict with either ``proofpgs_exe`` (pip install) or
    ``python_exe`` + ``project_dir`` (source / archive install).
    """
    if _is_pip_installed():
        exe = shutil.which("proofpgs")
        if not exe:
            print(status_err(
                "proofpgs is pip-installed but the 'proofpgs' command "
                "was not found on PATH."
            ), file=sys.stderr)
            sys.exit(1)
        return {"proofpgs_exe": exe}

    python_exe = sys.executable
    if not python_exe:
        print(status_err("Could not determine Python executable path."),
              file=sys.stderr)
        sys.exit(1)
    return {"python_exe": python_exe, "project_dir": _project_root()}


# ---------------------------------------------------------------------------
# Public API — platform dispatch
# ---------------------------------------------------------------------------

def install():
    """Register file manager context menu entries for ProofPGS."""
    if sys.platform == "win32":
        _install_windows()
    elif sys.platform == "linux":
        _install_linux()
    elif sys.platform == "darwin":
        _install_macos()
    else:
        print(status_err(
            f"Context menu integration is not supported on this "
            f"platform ({sys.platform})."
        ), file=sys.stderr)
        sys.exit(1)


def uninstall():
    """Remove file manager context menu entries for ProofPGS."""
    if sys.platform == "win32":
        _uninstall_windows()
    elif sys.platform == "linux":
        _uninstall_linux()
    elif sys.platform == "darwin":
        _uninstall_macos()
    else:
        print(status_err(
            f"Context menu integration is not supported on this "
            f"platform ({sys.platform})."
        ), file=sys.stderr)
        sys.exit(1)


# ===================================================================
# Windows — Explorer context menu via registry
# ===================================================================

def _icon_path_windows() -> str:
    """Return the icon path matching the current Windows theme."""
    import winreg
    variant = "light"  # light icon for dark backgrounds (default)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            if val == 1:
                variant = "dark"  # dark icon for light backgrounds
    except OSError:
        pass
    return str(Path(__file__).resolve().parent / "assets" / f"proofpgs-icon-{variant}.ico")


def _build_command_windows(mode: str, use_pause: bool, *,
                           proofpgs_exe: str | None = None,
                           python_exe: str | None = None,
                           project_dir: str | None = None) -> str:
    """Build the shell command string for a Windows context menu entry."""
    if proofpgs_exe:
        base = f'"{proofpgs_exe}" "%1" --mode {mode}'
    else:
        base = f'cd /d "{project_dir}" && "{python_exe}" -m proofpgs "%1" --mode {mode}'
    if use_pause:
        return f'cmd.exe /c "{base} & pause"'
    return f'cmd.exe /k "{base}"'


def _notify_shell_windows():
    """Tell Explorer to refresh file associations."""
    import ctypes
    SHCNE_ASSOCCHANGED = 0x08000000
    SHCNF_IDLIST = 0x0000
    ctypes.windll.shell32.SHChangeNotify(
        SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
    )


def _delete_key_tree(root, subkey: str):
    """Recursively delete a registry key and all its children."""
    import winreg
    try:
        hkey = winreg.OpenKey(root, subkey, 0,
                              winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return False

    # Enumerate and delete children first (depth-first)
    while True:
        try:
            child = winreg.EnumKey(hkey, 0)
            _delete_key_tree(root, f"{subkey}\\{child}")
        except OSError:
            break
    winreg.CloseKey(hkey)
    winreg.DeleteKey(root, subkey)
    return True


def _clean_empty_parents(root, subkey: str):
    """Walk up the path and remove empty parent keys."""
    import winreg
    parts = subkey.split("\\")
    while parts:
        path = "\\".join(parts)
        try:
            hkey = winreg.OpenKey(root, path, 0, winreg.KEY_READ)
            # Check if it has any subkeys or values
            info = winreg.QueryInfoKey(hkey)
            winreg.CloseKey(hkey)
            if info[0] == 0 and info[1] == 0:
                winreg.DeleteKey(root, path)
            else:
                break
        except OSError:
            break
        parts.pop()


def _install_windows():
    """Register Windows Explorer context menu entries for ProofPGS."""
    import winreg

    cmd_kwargs = _resolve_install_paths()

    sup_exts = sorted(SUP_EXTENSIONS)
    container_exts = sorted(CONTAINER_EXTENSIONS)

    # --- Create submenu commands for each extension group ---
    for submenu_key, modes in [(_SUBMENU_SUP, _SUP_MODES),
                                (_SUBMENU_CONTAINER, _CONTAINER_MODES)]:
        submenu_shell = f"Software\\Classes\\{submenu_key}\\shell"
        for reg_name, label, mode, use_pause in modes:
            verb_path = f"{submenu_shell}\\{reg_name}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, _win_label(label))

            cmd_path = f"{verb_path}\\command"
            command = _build_command_windows(mode, use_pause, **cmd_kwargs)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as key:
                winreg.SetValue(key, "", winreg.REG_SZ, command)

    # --- Register per-extension shell verb ---
    icon = _icon_path_windows()
    for ext, submenu_key in ([(e, _SUBMENU_SUP) for e in sup_exts] +
                              [(e, _SUBMENU_CONTAINER) for e in container_exts]):
        verb_path = (f"Software\\Classes\\SystemFileAssociations"
                     f"\\{ext}\\shell\\{_MENU_NAME}")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, verb_path) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, _MENU_NAME)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(key, "ExtendedSubCommandsKey", 0,
                              winreg.REG_SZ, submenu_key)

    _notify_shell_windows()

    pip_installed = "proofpgs_exe" in cmd_kwargs
    trailing = [
        "Windows 11: right-click and choose 'Show more options' "
        "to see the submenu.",
    ]
    if pip_installed:
        trailing.append(f"Command: {cmd_kwargs['proofpgs_exe']}")
    else:
        trailing.append(f"Python:  {cmd_kwargs['python_exe']}")
        trailing.append(f"Project: {cmd_kwargs['project_dir']}")
    _print_install_box("Context Menu Installed", sup_exts, container_exts,
                       post_lines=trailing)


def _uninstall_windows():
    """Remove all Windows Explorer context menu entries for ProofPGS."""
    import winreg

    removed = 0

    # --- Remove shared submenus ---
    for submenu_key in (_SUBMENU_SUP, _SUBMENU_CONTAINER):
        submenu_path = f"Software\\Classes\\{submenu_key}"
        if _delete_key_tree(winreg.HKEY_CURRENT_USER, submenu_path):
            removed += 1

    # --- Clean up legacy single submenu from older installs ---
    legacy_path = "Software\\Classes\\ProofPGS.SubMenu"
    if _delete_key_tree(winreg.HKEY_CURRENT_USER, legacy_path):
        removed += 1

    # --- Remove per-extension verbs ---
    for ext in _all_extensions():
        verb_path = (f"Software\\Classes\\SystemFileAssociations"
                     f"\\{ext}\\shell\\{_MENU_NAME}")
        if _delete_key_tree(winreg.HKEY_CURRENT_USER, verb_path):
            removed += 1
            # Clean up empty parent keys left behind
            shell_path = (f"Software\\Classes\\SystemFileAssociations"
                          f"\\{ext}\\shell")
            _clean_empty_parents(winreg.HKEY_CURRENT_USER, shell_path)

    _notify_shell_windows()

    if removed:
        print(status_ok(
            f"Removed {removed} Windows Explorer context menu entries."
        ))
    else:
        print("No ProofPGS context menu entries found.")


# ===================================================================
# Linux — freedesktop .desktop files
# ===================================================================

def _desktop_dir() -> Path:
    """Return ~/.local/share/applications/."""
    return Path.home() / ".local" / "share" / "applications"


def _mime_packages_dir() -> Path:
    """Return ~/.local/share/mime/packages/."""
    return Path.home() / ".local" / "share" / "mime" / "packages"


def _icon_path_linux() -> str:
    """Return absolute path to the PNG icon for .desktop files."""
    return str(Path(__file__).resolve().parent / "assets" / "proofpgs-icon-dark.png")


def _build_exec_linux(mode: str, *,
                      proofpgs_exe: str | None = None,
                      python_exe: str | None = None,
                      project_dir: str | None = None) -> str:
    """Build the Exec= value for a .desktop file."""
    if proofpgs_exe:
        return f'"{proofpgs_exe}" %f --mode {mode}'
    # Source/archive: need sh -c wrapper because Exec= doesn't support cd &&
    return (f"sh -c 'cd \"{project_dir}\" && "
            f"\"{python_exe}\" -m proofpgs \"$1\" --mode {mode}' _ %f")


def _desktop_entry(label: str, exec_cmd: str, mime_types: str,
                   icon: str) -> str:
    """Generate the content of a .desktop file."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name=ProofPGS - {label}\n"
        f"Exec={exec_cmd}\n"
        f"Icon={icon}\n"
        f"MimeType={mime_types};\n"
        "Terminal=true\n"
        "NoDisplay=true\n"
        "Categories=Utility;\n"
    )


_SUP_MIME_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-pgs-subtitle">
    <comment>PGS subtitle file</comment>
    <glob pattern="*.sup"/>
  </mime-type>
</mime-info>
"""


def _run_quiet(cmd: list[str]):
    """Run a command, suppressing errors if not found."""
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def _install_linux():
    """Register freedesktop .desktop context menu entries for ProofPGS."""
    paths = _resolve_install_paths()
    icon = _icon_path_linux()

    desktop_d = _desktop_dir()
    desktop_d.mkdir(parents=True, exist_ok=True)

    # --- Install custom MIME type for .sup ---
    mime_d = _mime_packages_dir()
    mime_d.mkdir(parents=True, exist_ok=True)
    (mime_d / "proofpgs-sup.xml").write_text(_SUP_MIME_XML, encoding="utf-8")
    _run_quiet(["update-mime-database",
                str(Path.home() / ".local" / "share" / "mime")])

    # --- Write .desktop files ---
    written = []
    for group, modes, mime in [("sup", _SUP_MODES, _SUP_MIME_TYPES),
                                ("container", _CONTAINER_MODES, _CONTAINER_MIME_TYPES)]:
        for reg_name, label, mode, _use_pause in modes:
            exec_cmd = _build_exec_linux(mode, **paths)
            content = _desktop_entry(label, exec_cmd, mime, icon)
            filename = f"proofpgs-{group}-{reg_name.split('_', 1)[1]}.desktop"
            (desktop_d / filename).write_text(content, encoding="utf-8")
            written.append(filename)

    _run_quiet(["update-desktop-database", str(desktop_d)])

    sup_exts = sorted(SUP_EXTENSIONS)
    container_exts = sorted(CONTAINER_EXTENSIONS)
    pip_installed = "proofpgs_exe" in paths
    trailing = [f"Installed {len(written)} .desktop files to {desktop_d}"]
    if pip_installed:
        trailing.append(f"Command: {paths['proofpgs_exe']}")
    else:
        trailing.append(f"Python:  {paths['python_exe']}")
        trailing.append(f"Project: {paths['project_dir']}")
    _print_install_box("Context Menu Installed", sup_exts, container_exts,
                       post_lines=trailing)


def _uninstall_linux():
    """Remove all freedesktop .desktop entries for ProofPGS."""
    desktop_d = _desktop_dir()
    removed = 0

    # Remove .desktop files
    for f in desktop_d.glob("proofpgs-*.desktop"):
        f.unlink(missing_ok=True)
        removed += 1

    # Remove custom MIME type
    mime_xml = _mime_packages_dir() / "proofpgs-sup.xml"
    if mime_xml.exists():
        mime_xml.unlink()
        _run_quiet(["update-mime-database",
                    str(Path.home() / ".local" / "share" / "mime")])

    if removed:
        _run_quiet(["update-desktop-database", str(desktop_d)])

    if removed:
        print(status_ok(f"Removed {removed} .desktop file(s) from {desktop_d}."))
    else:
        print("No ProofPGS context menu entries found.")


# ===================================================================
# macOS — Finder Quick Actions via Automator .workflow bundles
# ===================================================================

def _services_dir() -> Path:
    """Return ~/Library/Services/."""
    return Path.home() / "Library" / "Services"


def _build_shell_script_macos(mode: str, extensions: list[str], *,
                               proofpgs_exe: str | None = None,
                               python_exe: str | None = None,
                               project_dir: str | None = None) -> str:
    """Build the shell script that runs inside an Automator Quick Action.

    The script opens Terminal.app so the user sees CLI output.  An extension
    check guards against .sup workflows firing on non-.sup files (since .sup
    has no registered UTI and the workflow accepts ``public.data``).
    """
    if proofpgs_exe:
        cmd = f'{proofpgs_exe} "$f" --mode {mode}'
    else:
        cmd = (f'cd "{project_dir}" && '
               f'"{python_exe}" -m proofpgs "$f" --mode {mode}')

    # Build extension guard: only run if the file matches one of the expected
    # extensions.  This is critical for .sup where we can't filter by UTI.
    ext_checks = " || ".join(
        f'[ "${{f##*.}}" = "{ext.lstrip(".")}" ]' for ext in extensions
    )

    return (
        "for f in \"$@\"; do\n"
        f"    if {ext_checks}; then\n"
        f"        osascript -e 'tell application \"Terminal\"' "
        f"-e 'activate' "
        f"-e \"do script \\\"{cmd}\\\"\" "
        f"-e 'end tell'\n"
        "    fi\n"
        "done\n"
    )


def _info_plist(display_name: str) -> str:
    """Generate Info.plist for an Automator Quick Action."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
        '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>NSServices</key>\n'
        '    <array>\n'
        '        <dict>\n'
        '            <key>NSMenuItem</key>\n'
        '            <dict>\n'
        '                <key>default</key>\n'
        f'                <string>{display_name}</string>\n'
        '            </dict>\n'
        '            <key>NSMessage</key>\n'
        '            <string>runWorkflowAsService</string>\n'
        '        </dict>\n'
        '    </array>\n'
        '</dict>\n'
        '</plist>\n'
    )


def _document_wflow(shell_script: str, utis: list[str]) -> str:
    """Generate document.wflow for a 'Run Shell Script' Automator action.

    This produces the minimal Automator XML needed for a Quick Action that:
    - receives files/folders as input
    - filters by the given UTIs
    - runs a shell script with input passed as arguments
    """
    uti_entries = "\n".join(
        f"\t\t\t\t\t\t\t<string>{u}</string>" for u in utis
    )
    # Escape XML special characters in the shell script
    escaped_script = (shell_script
                      .replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
        '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '\t<key>AMApplicationBuild</key>\n'
        '\t<string>523</string>\n'
        '\t<key>AMApplicationVersion</key>\n'
        '\t<string>2.10</string>\n'
        '\t<key>AMDocumentVersion</key>\n'
        '\t<integer>2</integer>\n'
        '\t<key>actions</key>\n'
        '\t<array>\n'
        '\t\t<dict>\n'
        '\t\t\t<key>action</key>\n'
        '\t\t\t<dict>\n'
        '\t\t\t\t<key>AMAccepts</key>\n'
        '\t\t\t\t<dict>\n'
        '\t\t\t\t\t<key>Container</key>\n'
        '\t\t\t\t\t<string>List</string>\n'
        '\t\t\t\t\t<key>Optional</key>\n'
        '\t\t\t\t\t<false/>\n'
        '\t\t\t\t\t<key>Types</key>\n'
        '\t\t\t\t\t<array>\n'
        '\t\t\t\t\t\t<string>com.apple.cocoa.path</string>\n'
        '\t\t\t\t\t</array>\n'
        '\t\t\t\t</dict>\n'
        '\t\t\t\t<key>AMActionVersion</key>\n'
        '\t\t\t\t<string>2.0.3</string>\n'
        '\t\t\t\t<key>AMApplication</key>\n'
        '\t\t\t\t<array>\n'
        '\t\t\t\t\t<string>Automator</string>\n'
        '\t\t\t\t</array>\n'
        '\t\t\t\t<key>AMBundleIdentifier</key>\n'
        '\t\t\t\t<string>com.apple.RunShellScript</string>\n'
        '\t\t\t\t<key>AMCategory</key>\n'
        '\t\t\t\t<array>\n'
        '\t\t\t\t\t<string>AMCategoryUtilities</string>\n'
        '\t\t\t\t</array>\n'
        '\t\t\t\t<key>AMIconName</key>\n'
        '\t\t\t\t<string>RunShellScript</string>\n'
        '\t\t\t\t<key>AMKeywords</key>\n'
        '\t\t\t\t<array>\n'
        '\t\t\t\t\t<string>Shell</string>\n'
        '\t\t\t\t\t<string>Script</string>\n'
        '\t\t\t\t</array>\n'
        '\t\t\t\t<key>AMName</key>\n'
        '\t\t\t\t<string>Run Shell Script</string>\n'
        '\t\t\t\t<key>AMProvides</key>\n'
        '\t\t\t\t<dict>\n'
        '\t\t\t\t\t<key>Container</key>\n'
        '\t\t\t\t\t<string>List</string>\n'
        '\t\t\t\t\t<key>Types</key>\n'
        '\t\t\t\t\t<array>\n'
        '\t\t\t\t\t\t<string>com.apple.cocoa.string</string>\n'
        '\t\t\t\t\t</array>\n'
        '\t\t\t\t</dict>\n'
        '\t\t\t</dict>\n'
        '\t\t\t<key>class</key>\n'
        '\t\t\t<string>RunShellScriptAction</string>\n'
        '\t\t\t<key>isViewVisible</key>\n'
        '\t\t\t<true/>\n'
        '\t\t\t<key>location</key>\n'
        '\t\t\t<string>309.000000:253.000000</string>\n'
        '\t\t\t<key>nibPath</key>\n'
        '\t\t\t<string>/System/Library/Automator/Run Shell Script.action'
        '/Contents/Resources/Base.lproj/main.nib</string>\n'
        '\t\t\t<key>parameters</key>\n'
        '\t\t\t<dict>\n'
        '\t\t\t\t<key>COMMAND_STRING</key>\n'
        f'\t\t\t\t<string>{escaped_script}</string>\n'
        '\t\t\t\t<key>CheckedForUserDefaultShell</key>\n'
        '\t\t\t\t<true/>\n'
        '\t\t\t\t<key>inputMethod</key>\n'
        '\t\t\t\t<integer>1</integer>\n'
        '\t\t\t\t<key>shell</key>\n'
        '\t\t\t\t<string>/bin/bash</string>\n'
        '\t\t\t\t<key>source</key>\n'
        '\t\t\t\t<string></string>\n'
        '\t\t\t</dict>\n'
        '\t\t</dict>\n'
        '\t</array>\n'
        '\t<key>connectors</key>\n'
        '\t<dict/>\n'
        '\t<key>workflowMetaData</key>\n'
        '\t<dict>\n'
        '\t\t<key>applicationBundleIDsByPath</key>\n'
        '\t\t<dict/>\n'
        '\t\t<key>applicationPathsByBundleID</key>\n'
        '\t\t<dict/>\n'
        '\t\t<key>inputTypeIdentifier</key>\n'
        '\t\t<string>com.apple.Automator.fileSystemObject</string>\n'
        '\t\t<key>outputTypeIdentifier</key>\n'
        '\t\t<string>com.apple.Automator.nothing</string>\n'
        '\t\t<key>presentationMode</key>\n'
        '\t\t<integer>15</integer>\n'
        '\t\t<key>processesInput</key>\n'
        '\t\t<integer>0</integer>\n'
        '\t\t<key>serviceApplicationBundleID</key>\n'
        '\t\t<string>com.apple.finder</string>\n'
        '\t\t<key>serviceApplicationPath</key>\n'
        '\t\t<string>/System/Library/CoreServices/Finder.app</string>\n'
        '\t\t<key>serviceInputTypeIdentifier</key>\n'
        '\t\t<string>com.apple.Automator.fileSystemObject</string>\n'
        '\t\t<key>serviceOutputTypeIdentifier</key>\n'
        '\t\t<string>com.apple.Automator.nothing</string>\n'
        '\t\t<key>serviceProcessesInput</key>\n'
        '\t\t<integer>0</integer>\n'
        '\t\t<key>systemImageName</key>\n'
        '\t\t<string>NSActionTemplate</string>\n'
        '\t\t<key>useAutomaticInputType</key>\n'
        '\t\t<integer>0</integer>\n'
        '\t\t<key>workflowTypeIdentifier</key>\n'
        '\t\t<string>com.apple.Automator.servicesMenu</string>\n'
        '\t</dict>\n'
        '</dict>\n'
        '</plist>\n'
    )


def _install_macos():
    """Register macOS Finder Quick Actions for ProofPGS."""
    paths = _resolve_install_paths()
    services_d = _services_dir()
    services_d.mkdir(parents=True, exist_ok=True)

    written = []
    sup_exts = sorted(SUP_EXTENSIONS)
    container_exts = sorted(CONTAINER_EXTENSIONS)
    for group, modes, utis, extensions in [
        ("sup", _SUP_MODES, _SUP_UTIS, sup_exts),
        ("container", _CONTAINER_MODES, _CONTAINER_UTIS, container_exts),
    ]:
        for _reg_name, label, mode, _use_pause in modes:
            workflow_name = f"ProofPGS - {label} [{group}]"
            workflow_dir = services_d / f"{workflow_name}.workflow" / "Contents"
            workflow_dir.mkdir(parents=True, exist_ok=True)

            # Info.plist
            (workflow_dir / "Info.plist").write_text(
                _info_plist(f"ProofPGS - {label}"), encoding="utf-8")

            # Shell script + document.wflow
            script = _build_shell_script_macos(
                mode, extensions, **paths)
            (workflow_dir / "document.wflow").write_text(
                _document_wflow(script, utis), encoding="utf-8")

            written.append(workflow_name)

    # Refresh the Services menu
    _run_quiet(["/System/Library/CoreServices/pbs", "-update"])

    pip_installed = "proofpgs_exe" in paths
    trailing = [
        f"Installed {len(written)} Quick Actions to {services_d}",
        "Enable in System Settings > Privacy & Security > "
        "Extensions > Finder if needed.",
    ]
    if pip_installed:
        trailing.append(f"Command: {paths['proofpgs_exe']}")
    else:
        trailing.append(f"Python:  {paths['python_exe']}")
        trailing.append(f"Project: {paths['project_dir']}")
    _print_install_box("Quick Actions Installed", sup_exts, container_exts,
                       post_lines=trailing)


def _uninstall_macos():
    """Remove all macOS Finder Quick Actions for ProofPGS."""
    services_d = _services_dir()
    removed = 0

    for workflow in services_d.glob("ProofPGS - *.workflow"):
        # Remove the entire .workflow bundle (directory tree)
        shutil.rmtree(workflow, ignore_errors=True)
        removed += 1

    _run_quiet(["/System/Library/CoreServices/pbs", "-update"])

    if removed:
        print(status_ok(f"Removed {removed} Quick Action(s) from {services_d}."))
    else:
        print("No ProofPGS Quick Actions found.")

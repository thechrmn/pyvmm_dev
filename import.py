"""Stage 1 - find local video files and fingerprint their bytes with SHA-256.

The file is named ``import.py`` by design, so ``main.py`` loads it with
``importlib.import_module("import")``. Stages exchange plain dictionaries only,
so no other module imports this one.
"""

import hashlib
import os
import stat
from pathlib import Path

VIDEO_EXTENSIONS = frozenset(
    ".avi .mp4 .mov .mkv .m4v .mpg .mpeg .webm .wmv .flv .mts .m2ts .vob "
    ".ogv .mjpeg .mjpg .mj2 .h264 .h265 .hevc .3gp .3g2 .ts .mxf .nut .dv .y4m .ivf .m2v".split()
)


def _issue(issues, code, message, path=None, severity="warning"):
    issue = {"severity": severity, "code": code, "message": message, "stage": "import"}
    if path is not None:
        issue["path"] = str(path)
    issues.append(issue)


def _is_link(path):
    """True for symbolic links and Windows junctions (mount points)."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == 0xA0000003


def fingerprint_file(path, relative_path):
    """Return the asset record of one regular file.

    Raises OSError or ValueError if the path is not a regular file or its size or
    modification time changed while it was being read.
    """
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"File changed while it was being read: {path}")
    return {
        "path": str(path),
        "relative_path": relative_path,
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _walk(root, wanted, issues):
    """Yield matching files below root; links inside the tree are reported, never followed."""

    def unreadable(error):
        _issue(issues, "folder_unreadable", str(error), error.filename, "error")

    for current, dirs, files in os.walk(root, onerror=unreadable):
        folder = Path(current)
        dirs.sort()
        for name in [d for d in dirs if _is_link(folder / d)]:
            dirs.remove(name)
            _issue(issues, "symlink_skipped", "Linked folder was not followed.", folder / name)
        for name in sorted(files):
            path = folder / name
            if wanted is not None and path.suffix.lower() not in wanted:
                continue
            if _is_link(path):
                _issue(issues, "symlink_skipped", "Linked file was not imported.", path)
            else:
                yield path


def _collect(inputs, wanted, issues):
    """Resolve the inputs: return (ordered set of files to import, folders they are found under)."""
    found, anchors = {}, []
    for item in inputs:
        try:
            root = Path(item).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            _issue(issues, "input_not_found", f"{type(error).__name__}: {error}", item, "error")
            continue
        if root.is_dir():
            anchors.append(root)
            found.update(dict.fromkeys(_walk(root, wanted, issues)))
        elif root.is_file():
            anchors.append(root.parent)
            found[root] = None
        else:
            _issue(issues, "input_unsupported", "Not a file or folder.", item, "error")
    return found, anchors


def import_files(inputs, issues, *, extensions=VIDEO_EXTENSIONS):
    """Yield one fingerprinted asset per distinct input file, in a stable order.

    ``inputs`` are files or folders. Paths you name are resolved, so they may reach
    the data through links. Folders are searched recursively for ``extensions``
    (``"*"`` accepts every file); explicit files are always imported. Problems are
    appended to ``issues`` (the list is complete once the generator is exhausted).
    """
    wanted = None if "*" in extensions else frozenset("." + e.lstrip(".").lower() for e in extensions)
    found, anchors = _collect(inputs, wanted, issues)
    try:
        base = Path(os.path.commonpath(anchors)) if anchors else None
    except ValueError:  # inputs on different drives
        base = None
    seen, imported = {}, False
    for path in sorted(found, key=lambda p: (str(p).casefold(), str(p))):
        relative = path.relative_to(base).as_posix() if base else path.as_posix()
        try:
            asset = fingerprint_file(path, relative)
        except (OSError, ValueError) as error:
            _issue(issues, "file_unreadable", f"{type(error).__name__}: {error}", path, "error")
            continue
        first = seen.setdefault(asset["sha256"], relative)
        if first != relative:
            _issue(issues, "duplicate_content", f"Identical bytes to {first}.", path)
        imported = True
        yield asset
    if not imported:
        _issue(issues, "no_inputs", "No readable video files were found.", severity="error")

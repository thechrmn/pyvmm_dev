"""Discover local recordings and fingerprint their unchanged bytes."""

import hashlib
import os
import stat
from pathlib import Path

VIDEO_EXTENSIONS = frozenset(
    ".avi .mp4 .mov .mkv .m4v .mpg .mpeg .webm .wmv .flv .mts .m2ts .vob "
    ".ogv .mjpeg .mjpg .h264 .h265 .hevc .3gp .ts .mxf .nut".split()
)


def _is_link(path):
    try:
        value = path.lstat()
        return stat.S_ISLNK(value.st_mode) or getattr(value, "st_reparse_tag", 0) == 0xA0000003
    except FileNotFoundError:
        return False


def file_identity(value):
    """One source snapshot shared with extraction."""
    return dict(
        size_bytes=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        device_id=value.st_dev,
        inode=value.st_ino,
    )


def fingerprint_file(path, relative_path):
    """Hash a regular file with bounded memory and observable-change checks."""
    path = Path(path).expanduser().absolute()
    if any(_is_link(part) for part in (path, *path.parents)):
        raise ValueError(f"Symbolic link or junction: {path}")

    def signature(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or signature(before) != signature(opened):
                raise ValueError(f"Input changed before hashing: {path}")
            remaining = opened.st_size
            buffer = memoryview(bytearray(min(1024 * 1024, remaining)))
            while remaining:
                count = stream.readinto(buffer[:remaining])
                if not count:
                    raise ValueError(f"Input truncated during hashing: {path}")
                digest.update(buffer[:count])
                remaining -= count
            if stream.read(1):
                raise ValueError(f"Input grew during hashing: {path}")
            after = os.fstat(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    current = path.stat()
    # Windows stat/fstat can differ in ctime; compare it only within each API.
    if (
        signature(opened) != signature(after)
        or signature(after) != signature(current)
        or before.st_ctime_ns != current.st_ctime_ns
        or opened.st_ctime_ns != after.st_ctime_ns
    ):
        raise ValueError(f"Input changed during hashing: {path}")
    return {
        "path": str(path),
        "relative_path": relative_path,
        **file_identity(current),
        "sha256": digest.hexdigest(),
    }


def import_files(inputs, *, exclude_paths=(), extensions=VIDEO_EXTENSIONS):
    """Return sorted source assets and issues; a bad input cannot hide good ones."""
    issues, roots, candidates = [], [], set()
    if isinstance(extensions, str):
        extensions = None if extensions.strip() == "*" else extensions.split(",")
    suffixes = (
        None
        if extensions is None
        else {"." + suffix.strip().lstrip(".").lower() for suffix in extensions}
    )
    excluded = [Path(path).expanduser().resolve() for path in exclude_paths]

    def report(severity, code, message, path, **details):
        issues.append(
            dict(
                severity=severity,
                code=code,
                message=message,
                stage="import",
                details={"path": str(path), **details},
            )
        )

    def usable(path):
        try:
            if _is_link(path):
                report("warning", "symlink_skipped", "Symbolic link or junction skipped", path)
                return False
            return not any(path.is_relative_to(parent) for parent in excluded)
        except (OSError, ValueError, RuntimeError) as error:
            report("error", "input_unreadable", str(error), path)
            return False

    def add(path):
        if usable(path):
            if path in candidates:
                report("info", "duplicate_path", "Repeated path deduplicated", path)
            candidates.add(path)

    def walk_error(error):
        report("error", "directory_unreadable", str(error), error.filename)

    for supplied in inputs:
        path = Path(supplied)
        try:
            path = path.expanduser().absolute()
            if any(_is_link(part) for part in (path, *path.parents)):
                report("warning", "symlink_skipped", "Symbolic link or junction skipped", path)
                continue
            path = path.resolve(strict=True)
            if any(path.is_relative_to(parent) for parent in excluded):
                report("error", "input_excluded", "Input is inside the output directory", path)
            elif path.is_file():
                roots.append(path.parent)
                add(path)
            elif path.is_dir():
                roots.append(path)
                for directory, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
                    base = Path(directory)
                    dirs[:] = [name for name in sorted(dirs) if usable(base / name)]
                    for name in sorted(files):
                        if suffixes is None or Path(name).suffix.lower() in suffixes:
                            add(base / name)
            else:
                report("error", "not_regular_file", "Not a regular file or directory", path)
        except (OSError, ValueError, RuntimeError) as error:
            report("error", "input_unreadable", str(error), path)
    try:
        common = Path(os.path.commonpath(roots)) if roots else None
    except ValueError:
        common = None  # Different Windows drives: use absolute selectors.
    assets, seen_hashes = [], {}
    for path in sorted(candidates, key=lambda value: (str(value).casefold(), str(value))):
        try:
            relative = path.relative_to(common).as_posix() if common else path.as_posix()
            asset = fingerprint_file(path, relative)
            if asset["sha256"] in seen_hashes:
                report(
                    "warning",
                    "duplicate_content",
                    "Identical bytes; retaining both paths",
                    path,
                    first_path=seen_hashes[asset["sha256"]],
                )
            seen_hashes.setdefault(asset["sha256"], str(path))
            assets.append(asset)
        except (OSError, ValueError, RuntimeError) as error:
            report("error", "fingerprint_failed", str(error), path)
    if not assets:
        report("error", "no_inputs", "No readable video inputs discovered", "")
    return assets, issues

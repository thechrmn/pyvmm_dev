"""Deterministic local discovery and content fingerprints (stage 1)."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path

VIDEO_EXTENSIONS = frozenset(
    ".avi .mp4 .mov .mkv .m4v .mpg .mpeg .webm .wmv .flv .mts .m2ts .vob "
    ".ogv .mjpeg .mjpg .h264 .h265 .hevc .3gp .ts .mxf .nut".split()
)


def _report(issues: list, severity: str, code: str, message: str, **details) -> None:
    issues.append(
        dict(severity=severity, code=code, message=message, stage="import", details=details)
    )


def _is_link(path: Path) -> bool:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    # The junction tag also works on Python 3.11 without rejecting OneDrive's
    # cloud-placeholder reparse points. One lstat handles both alias types.
    return stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_reparse_tag", None) == 0xA0000003


def _has_alias(path: Path) -> bool:
    return any(_is_link(part) for part in (path, *path.parents))


def fingerprint_file(path: Path, relative_path: str) -> dict:
    """Hash a regular file with constant memory; reject observable concurrent writes."""
    path = Path(path).expanduser().absolute()
    if _has_alias(path):
        raise ValueError(f"Input contains a symbolic link or junction: {path}")

    def signature(s):
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns

    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1  # The stream now owns the descriptor.
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"Input is no longer a regular file: {path}")
            if signature(before) != signature(opened):
                raise ValueError(f"Input changed before hashing: {path}")
            remaining = opened.st_size
            buffer = memoryview(bytearray(min(1024 * 1024, remaining)))
            while remaining:
                count = stream.readinto(buffer[:remaining])
                if not count:
                    raise ValueError(f"Input was truncated during hashing: {path}")
                digest.update(buffer[:count])
                remaining -= count
            if stream.read(1):
                raise ValueError(f"Input grew during hashing: {path}")
            after = os.fstat(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    current = path.stat()

    # On Windows path.stat and os.fstat can expose different ctime semantics.
    # Compare ctime only within the same API, and identity/size/mtime across APIs.
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
        "size_bytes": current.st_size,
        "mtime_ns": current.st_mtime_ns,
        "ctime_ns": current.st_ctime_ns,
        "device_id": current.st_dev,
        "inode": current.st_ino,
        "sha256": digest.hexdigest(),
    }


def _discover_files(inputs, excluded, issues):
    """Discover source paths without reading their contents."""
    candidates: set[Path] = set()
    roots: list[Path] = []

    def excluded_path(path: Path) -> bool:
        return any(path.is_relative_to(parent) for parent in excluded)

    def add_file(path: Path) -> None:
        try:
            if _is_link(path):
                _report(
                    issues, "warning", "symlink_skipped", "Symbolic link skipped", path=str(path)
                )
                return
            resolved = path.resolve()
            if excluded_path(resolved):
                return
            if resolved in candidates:
                _report(
                    issues,
                    "info",
                    "duplicate_path",
                    "Repeated input path deduplicated",
                    path=str(resolved),
                )
            candidates.add(resolved)
        except (OSError, ValueError, RuntimeError) as error:
            _report(issues, "error", "input_unreadable", str(error), path=str(path))

    def keep_directory(path: Path) -> bool:
        try:
            if _is_link(path):
                _report(
                    issues, "warning", "symlink_skipped", "Directory alias skipped", path=str(path)
                )
                return False
            return not excluded_path(path.resolve())
        except (OSError, ValueError, RuntimeError) as error:
            _report(issues, "error", "directory_unreadable", str(error), path=str(path))
            return False

    def walk_error(error: OSError) -> None:
        _report(issues, "error", "directory_unreadable", str(error), path=str(error.filename))

    for supplied in inputs:
        path = Path(supplied)
        try:
            path = path.expanduser().absolute()
            if _has_alias(path):
                _report(
                    issues, "warning", "symlink_skipped", "Symbolic link skipped", path=str(path)
                )
                continue
            resolved = path.resolve(strict=True)
            if excluded_path(resolved):
                _report(
                    issues,
                    "error",
                    "input_excluded",
                    "Input is within the output directory",
                    path=str(resolved),
                )
                continue
            if resolved.is_file():
                roots.append(resolved.parent)
                add_file(resolved)
            elif resolved.is_dir():
                roots.append(resolved)
                for directory, dirs, files in os.walk(
                    resolved, followlinks=False, onerror=walk_error
                ):
                    base = Path(directory)
                    dirs[:] = [d for d in sorted(dirs) if keep_directory(base / d)]
                    for name in sorted(files):
                        if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                            add_file(base / name)
            else:
                _report(
                    issues,
                    "error",
                    "not_regular_file",
                    "Input is not a regular file or directory",
                    path=str(resolved),
                )
        except (OSError, ValueError, RuntimeError) as error:
            _report(issues, "error", "input_unreadable", str(error), path=str(path))
    return candidates, roots


def import_files(
    inputs: Iterable[str | Path],
    *,
    exclude_paths: Iterable[str | Path] = (),
) -> tuple[list[dict], list[dict]]:
    """Discover videos, leaving duplicates by content as distinct file records.

    Explicit files are probed regardless of extension. Directories use a suffix
    allowlist. Symlinks/junctions are skipped; regular hardlinks are independent
    paths. All failures are collected so a bad input cannot conceal good files.
    """
    issues: list[dict] = []
    excluded = [Path(p).expanduser().resolve() for p in exclude_paths]
    candidates, roots = _discover_files(inputs, excluded, issues)
    try:
        common = Path(os.path.commonpath(roots)) if roots else None
    except ValueError:  # e.g. multiple Windows drives: use absolute POSIX selectors.
        common = None
    assets: list[dict] = []
    seen_hashes: dict[str, str] = {}
    for path in sorted(candidates, key=lambda p: (str(p).casefold(), str(p))):
        try:
            relative = path.relative_to(common).as_posix() if common else path.as_posix()
            asset = fingerprint_file(path, relative)
            if asset["sha256"] in seen_hashes:
                _report(
                    issues,
                    "warning",
                    "duplicate_content",
                    "Identical bytes at multiple paths; retaining both records",
                    path=str(path),
                    first_path=seen_hashes[asset["sha256"]],
                )
            seen_hashes.setdefault(asset["sha256"], str(path))
            assets.append(asset)
        except (OSError, ValueError, RuntimeError) as error:
            _report(issues, "error", "fingerprint_failed", str(error), path=str(path))
    if not assets:
        _report(issues, "error", "no_inputs", "No readable video inputs discovered")
    return assets, issues

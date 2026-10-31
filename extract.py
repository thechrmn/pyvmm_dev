"""Extract local video metadata with bounded FFprobe execution and exact timing evidence."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

# Exclude resource-expanding playlists/sequences: only this file is fingerprinted.
# MOV external-track references remain disabled by FFmpeg's enable_drefs=0 default.
SUPPORTED_DEMUXERS = tuple(
    "3g2 3gp aac asf avi flv h264 hevc m4a m4v matroska mj2 mjpeg mov mp3 mp4 "
    "mpeg mpegts mpegvideo mxf nut ogg wav webm".split()
)
_STAT_FIELDS = {
    "size_bytes": "st_size",
    "mtime_ns": "st_mtime_ns",
    "device_id": "st_dev",
    "inode": "st_ino",
    "ctime_ns": "st_ctime_ns",
}


class ProbeFailure(Exception):
    """A failed or resource-bounded external probe."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _limits(timeout, limit):
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("output limit must be a positive integer")


def _pipe_reader(pipes):
    """Return a nonblocking reader for POSIX or Windows/Python 3.11 pipes."""
    if os.name != "nt":
        for pipe in pipes:
            os.set_blocking(pipe.fileno(), False)

        def read(pipe, amount):
            try:
                return os.read(pipe.fileno(), amount)
            except BlockingIOError:
                return None

        return read

    import ctypes
    import msvcrt
    from ctypes import wintypes

    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    peek.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    peek.restype = wintypes.BOOL

    def read(pipe, amount):
        available = wintypes.DWORD()
        if not peek(
            msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(available), None
        ):
            error = ctypes.get_last_error()
            if error in (109, 233):  # broken/disconnected pipe: EOF
                return b""
            raise OSError(error, "PeekNamedPipe failed")
        return os.read(pipe.fileno(), min(amount, available.value)) if available.value else None

    return read


def _close_process(process, pipes):
    try:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
    finally:
        for pipe in pipes:
            pipe.close()


def _run_bounded(command: list[str], timeout: float, limit: int) -> tuple[int, bytes, bytes]:
    """Bound combined captured bytes and the lifetime of both output pipes.

    No reader threads remain blocked when an inherited writer outlives the tool.
    Reads inspect at most one byte beyond the budget solely to detect overflow.
    """
    _limits(timeout, limit)
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as error:
        raise ProbeFailure(
            "ffprobe_not_found", f"FFprobe executable not found: {command[0]}"
        ) from error
    except OSError as error:
        raise ProbeFailure("ffprobe_launch_failed", str(error)) from error

    pipes = [process.stdout, process.stderr]
    buffers, opened, total = [bytearray(), bytearray()], {0, 1}, 0
    try:
        read = _pipe_reader(pipes)
        while opened or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(
                    "ffprobe_timeout", f"FFprobe exceeded the {timeout:g} second time limit."
                )
            progress = False
            for index in tuple(opened):
                chunk = read(pipes[index], min(65536, limit - total + 1))
                if chunk is None:
                    continue
                progress = True
                if not chunk:
                    opened.remove(index)
                elif total + len(chunk) > limit:
                    raise ProbeFailure(
                        "ffprobe_output_limit", f"FFprobe exceeded the {limit} byte output limit."
                    )
                else:
                    buffers[index].extend(chunk)
                    total += len(chunk)
            if not progress:
                time.sleep(min(0.01, remaining))
        return process.returncode, bytes(buffers[0]), bytes(buffers[1])
    except OSError as error:
        raise ProbeFailure(
            "ffprobe_read_failed", f"Could not read FFprobe output: {error}"
        ) from error
    finally:
        _close_process(process, pipes)


def _native_tool(executable):
    if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("Use a native FFprobe executable, not a batch script.")


def inspect_ffprobe(ffprobe: str = "ffprobe", *, timeout: float = 10) -> dict:
    """Resolve and inspect the native backend before expensive file import."""
    executable = shutil.which(os.fspath(ffprobe))
    if executable is None:
        raise ValueError(f"FFprobe executable not found: {ffprobe}")
    executable = str(Path(executable).resolve(strict=True))
    _native_tool(executable)
    try:
        code, output, diagnostic = _run_bounded([executable, "-version"], timeout, 65536)
    except ProbeFailure as error:
        raise ValueError(f"Cannot inspect FFprobe: {error}") from error
    banner = output.decode("utf-8", errors="replace").splitlines()
    match = re.fullmatch(r"ffprobe version (\S+)(?: .*|)", banner[0]) if banner else None
    if code or match is None:
        reason = diagnostic.decode("utf-8", errors="replace")[:500]
        raise ValueError(
            f"The selected executable is not a working FFprobe: {executable}. {reason}"
        )
    return {
        "name": "ffprobe",
        "executable": executable,
        "version": match.group(1),
        "banner": banner[0],
    }


def _fraction(value: Any, *, allow_zero: bool = False, signed: bool = False) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if len(text) > 128:
        return None
    exponent = re.search(r"[eE]([+-]?\d+)$", text)
    if exponent is not None and abs(int(exponent.group(1))) > 400:
        return None  # Bound scientific notation before constructing huge integers.
    try:
        result = Fraction(text)
        numeric = float(result)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(numeric) or (numeric == 0 and result != 0):
        return None
    if not signed and (result < 0 or (result == 0 and not allow_zero)):
        return None
    return result


def _integer(value, *, allow_zero=False):
    fraction = _fraction(value, allow_zero=allow_zero)
    return fraction.numerator if fraction is not None and fraction.denominator == 1 else None


def _issue(issues, code, message, severity="warning"):
    issues.append({"severity": severity, "code": code, "message": message, "stage": "extract"})


def _streams(raw):
    streams = raw.get("streams", [])
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise ValueError("FFprobe streams must be a list of objects.")
    indices = [s.get("index") for s in streams]
    if any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(indices):
        raise ValueError("FFprobe stream indices must be unique nonnegative integers.")
    for stream in streams:
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict) or any(
            type(disposition.get(k, 0)) is not int or disposition.get(k, 0) not in (0, 1)
            for k in ("attached_pic", "default")
        ):
            raise ValueError("FFprobe dispositions must contain 0/1 integer flags.")
    return streams


def _video(streams, issues):
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if not videos:
        _issue(issues, "video_stream_not_found", "No eligible video stream was found.", "error")
        return None
    if len(videos) > 1:
        _issue(
            issues,
            "multiple_video_streams",
            "Multiple video streams exist; selected the default stream, then lowest stream index.",
        )
    return min(videos, key=lambda s: (not s.get("disposition", {}).get("default"), s["index"]))


def _frame_rate(stream, issues):
    average, nominal = (
        _fraction(stream.get("avg_frame_rate")),
        _fraction(stream.get("r_frame_rate")),
    )
    rate = average or nominal
    if rate is None:
        return {}
    if average is None:
        _issue(
            issues,
            "nominal_frame_rate",
            "Average frame rate is unavailable; using the nominal frame rate, which may differ from observed timing.",
        )
    elif nominal is not None and average != nominal:
        _issue(
            issues,
            "possible_variable_frame_rate",
            "Average and nominal frame rates differ; this may indicate variable frame timing. No constant-rate assumption was made.",
        )
    return {
        "frame_rate": float(rate),
        "frame_rate_rational": f"{rate.numerator}/{rate.denominator}",
        "frame_rate_source": "stream.avg_frame_rate" if average else "stream.r_frame_rate",
    }


def _duration(stream, format_data, streams, issues):
    # Prefer exact timestamps to FFprobe's usually six-digit decimal display.
    ticks, base = (
        _integer(stream.get("duration_ts"), allow_zero=True),
        _fraction(stream.get("time_base")),
    )
    exact = ticks * base if ticks is not None and base is not None else None
    reported = _fraction(stream.get("duration"), allow_zero=True)
    if (
        exact is not None
        and reported is not None
        and abs(exact - reported) > Fraction(1, 1_000_000)
    ):
        _issue(
            issues,
            "duration_mismatch",
            "Stream duration and duration_ts/time_base disagree beyond FFprobe's microsecond display precision; retaining the exact timestamp-based duration.",
        )
    duration = exact if exact is not None else reported
    source = "stream.duration_ts*time_base" if exact is not None else "stream.duration"
    result = {}
    if duration is None:
        container = _fraction(format_data.get("duration"), allow_zero=True)
        if container is None:
            return result
        result["container_duration_seconds"] = float(container)
        timed = sum(
            s.get("codec_type") != "attachment" and not s.get("disposition", {}).get("attached_pic")
            for s in streams
        )
        if timed != 1:
            _issue(
                issues,
                "video_duration_ambiguous",
                "Video stream duration is unavailable; the multi-stream container duration is retained separately and cannot establish this video's recording duration.",
            )
            return result
        duration, source = container, "format.duration"
        _issue(
            issues,
            "container_duration_fallback",
            "Video stream duration is unavailable; using the single-stream container duration.",
        )
    try:
        numeric = float(duration)
    except OverflowError:
        return result
    if math.isfinite(numeric) and (numeric != 0 or duration == 0):
        result.update(
            duration_seconds=numeric,
            duration_rational=f"{duration.numerator}/{duration.denominator}",
            duration_source=source,
        )
    return result


def _technical(raw: dict, issues: list[dict]) -> dict:
    try:
        streams = _streams(raw)
    except ValueError as error:
        _issue(issues, "invalid_probe_structure", str(error), "error")
        return {}
    stream = _video(streams, issues)
    if stream is None:
        return {}
    result = {"stream_index": stream["index"]}
    for field in ("width", "height"):
        value = _integer(stream.get(field))
        if value is not None:
            result[field] = value
    for source, target in (("codec_name", "codec"), ("pix_fmt", "pixel_format")):
        if isinstance(stream.get(source), str) and stream[source]:
            result[target] = stream[source]
    format_data = raw.get("format", {})
    if not isinstance(format_data, dict):
        _issue(issues, "invalid_probe_structure", "FFprobe format must be an object.", "error")
        format_data = {}
    if isinstance(format_data.get("format_name"), str):
        result["container"] = format_data["format_name"]
    result.update(_frame_rate(stream, issues))
    result.update(_duration(stream, format_data, streams, issues))
    frames = _integer(stream.get("nb_frames"), allow_zero=True)
    if frames is not None:
        result.update(frame_count=frames, frame_count_source="stream.nb_frames")
    start = _fraction(stream.get("start_time"), allow_zero=True, signed=True)
    if start is not None:
        result["start_time_seconds"] = float(start)
    depth = _integer(stream.get("bits_per_raw_sample"))
    if depth is not None:  # bits per pixel is not bit depth per channel.
        result["bit_depth"] = depth
    pixel_format = result.get("pixel_format", "")
    if pixel_format.startswith(("gray", "mono")):
        result["color_mode"] = "grayscale"
    elif pixel_format.startswith(("rgb", "bgr", "gbr")):
        result["color_mode"] = "rgb"
    for key in ("width", "height", "frame_rate", "duration_seconds"):
        if key not in result:
            _issue(issues, "technical_value_unavailable", f"No usable {key} was reported.")
    return result


def _identity(observed):
    return {key: getattr(observed, attribute) for key, attribute in _STAT_FIELDS.items()}


def _source(asset, issues):
    try:
        path = Path(asset["path"]).resolve(strict=True)
        observed = path.stat()
        if not stat.S_ISREG(observed.st_mode):
            raise OSError("Input is not a regular file")
        if any(asset.get(key, value) != value for key, value in _identity(observed).items()):
            _issue(
                issues,
                "input_changed",
                "Input changed after import; re-import it before extraction.",
                "error",
            )
            return None
        return path, observed
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        _issue(issues, "input_unavailable", str(error), "error")
        return None


def _check_source(path, before, issues):
    try:
        after = path.stat()
        if not stat.S_ISREG(after.st_mode) or _identity(before) != _identity(after):
            _issue(
                issues,
                "input_changed",
                "Input changed during extraction; metadata and checksum may no longer describe the same content.",
                "error",
            )
    except OSError as error:
        _issue(
            issues, "input_changed", f"Input became unavailable during extraction: {error}", "error"
        )


def _decode_probe(output):
    def reject_constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Nonfinite JSON number")
        return parsed

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key in FFprobe response: {key}")
            result[key] = value
        return result

    raw = json.loads(
        output.decode("utf-8"),
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=unique_pairs,
    )
    if not isinstance(raw, dict):
        raise ValueError("FFprobe response must be a JSON object")
    return raw


def _read_response(result, response):
    code, output, stderr = response
    issues = result["issues"]
    diagnostic = stderr.decode("utf-8", errors="replace")
    if stderr:
        result["tool"]["stderr"] = diagnostic
    if code:
        _issue(
            issues,
            "ffprobe_failed",
            f"FFprobe exited with code {code}: {diagnostic[:2000]}",
            "error",
        )
        if not output.strip():
            return
    try:
        result["raw"] = _decode_probe(output)
        version = result["raw"].get("program_version")
        if isinstance(version, dict):
            result["tool"]["version"] = version.get("version")
        result["technical"] = _technical(result["raw"], issues)
    except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as error:
        _issue(issues, "invalid_probe_json", str(error), "error")
    if stderr:
        _issue(
            issues,
            "ffprobe_diagnostic",
            "FFprobe reported diagnostics; see tool.stderr for the complete output.",
        )


def extract_metadata(
    asset: dict,
    *,
    ffprobe: str = "ffprobe",
    timeout: float = 60,
    max_output_bytes: int = 16_777_216,
) -> dict:
    """Extract one imported asset; operational failures become per-file issues.

    Basic probing may decode an initial sample to identify the video; no
    complete scan or synthetic frame count is requested.
    Container creation times are never promoted to experimental timestamps.
    """
    _limits(timeout, max_output_bytes)
    executable = os.fspath(ffprobe)
    _native_tool(executable)
    result = {
        "asset": dict(asset),
        "raw": {},
        "technical": {},
        "issues": [],
        "tool": {"name": "ffprobe", "executable": executable},
    }
    source = _source(asset, result["issues"])
    if source is None:
        return result
    path, before = source
    command = [
        executable,
        "-v",
        "warning",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(SUPPORTED_DEMUXERS),
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-show_programs",
        "-show_program_version",
        "-show_library_versions",
    ]
    command.extend(["-i", str(path)])
    result["tool"]["command"] = command
    try:
        response = _run_bounded(command, timeout, max_output_bytes)
        _read_response(result, response)
    except ProbeFailure as error:
        _issue(result["issues"], error.code, str(error), "error")
    finally:
        _check_source(path, before, result["issues"])
    return result

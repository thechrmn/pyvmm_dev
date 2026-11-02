"""Read local video metadata without decoding or inventing acquisition facts."""

import base64
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from fractions import Fraction
from importlib import import_module
from pathlib import Path

file_identity = import_module("import").file_identity
SUPPORTED_DEMUXERS = (
    "3g2 3gp aac asf avi flv h264 hevc m4a m4v matroska mj2 mjpeg mov mp3 mp4 "
    "mpeg mpegts mpegvideo mxf nut ogg wav webm"
).split()
_SECTIONS = ("format", "streams", "chapters", "programs", "program_version", "library_versions")


class ProbeFailure(Exception):
    """An operational probe failure, reported on the affected file."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _pipe_reader(pipes):
    """Read both pipes without leaving blocked threads after cancellation."""
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
            if error in (109, 233):
                return b""
            raise OSError(error, "PeekNamedPipe failed")
        return os.read(pipe.fileno(), min(amount, available.value)) if available.value else None

    return read


def _run_bounded(command, timeout, limit):
    """Limit elapsed time and combined stdout/stderr, including inherited writers."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number")
    if type(limit) is not int or limit <= 0:
        raise ValueError("output limit must be a positive integer")
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
    except OSError as error:
        code = (
            "ffprobe_not_found" if isinstance(error, FileNotFoundError) else "ffprobe_launch_failed"
        )
        raise ProbeFailure(code, str(error)) from error
    pipes = [process.stdout, process.stderr]
    buffers, opened, total = [bytearray(), bytearray()], {0, 1}, 0
    try:
        read = _pipe_reader(pipes)
        while opened or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure("ffprobe_timeout", f"FFprobe exceeded {timeout:g} seconds.")
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
                        "ffprobe_output_limit", f"FFprobe exceeded {limit} output bytes."
                    )
                else:
                    buffers[index].extend(chunk)
                    total += len(chunk)
            if not progress:
                time.sleep(min(0.01, remaining))
        return process.returncode, bytes(buffers[0]), bytes(buffers[1])
    except OSError as error:
        raise ProbeFailure("ffprobe_read_failed", str(error)) from error
    finally:
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


def _native_tool(executable):
    if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("Use a native FFprobe executable, not a batch script.")


def inspect_ffprobe(ffprobe="ffprobe", *, timeout=10):
    """Inspect the backend before importing large recordings."""
    executable = shutil.which(os.fspath(ffprobe))
    if executable is None:
        raise ValueError(f"FFprobe executable not found: {ffprobe}")
    executable = str(Path(executable).resolve(strict=True))
    _native_tool(executable)
    try:
        code, output, diagnostic = _run_bounded([executable, "-version"], timeout, 65536)
    except ProbeFailure as error:
        raise ValueError(f"Cannot inspect FFprobe: {error}") from error
    lines = output.decode("utf-8", errors="replace").splitlines()
    match = re.fullmatch(r"ffprobe version (\S+)(?: .*|)", lines[0]) if lines else None
    if code or match is None:
        raise ValueError(
            f"Not a working FFprobe: {executable}. "
            + diagnostic.decode("utf-8", errors="replace")[:500]
        )
    return {
        "name": "ffprobe",
        "executable": executable,
        "version": match[1],
        "banner": lines[0],
    }


def _fraction(value, *, allow_zero=False, signed=False):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if len(text) > 128:
        return None
    exponent = re.search(r"[eE]([+-]?\d+)$", text)
    if exponent and abs(int(exponent[1])) > 400:
        return None
    try:
        result = Fraction(text)
        number = float(result)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(number) or (number == 0 and result != 0):
        return None
    return result if signed or (result > 0 or (allow_zero and result == 0)) else None


def _integer(value, *, allow_zero=False):
    number = _fraction(value, allow_zero=allow_zero)
    return number.numerator if number is not None and number.denominator == 1 else None


def _issue(issues, code, message, severity="warning"):
    issues.append(dict(severity=severity, code=code, message=message, stage="extract"))


def _technical(raw, issues):
    streams = raw.get("streams", [])
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise ValueError("FFprobe streams must be a list of objects.")
    indices = [s.get("index") for s in streams]
    if any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(indices):
        raise ValueError("FFprobe stream indices must be unique nonnegative integers.")
    for stream in streams:
        flags = stream.get("disposition", {})
        if not isinstance(flags, dict) or any(
            type(flags.get(k, 0)) is not int or flags.get(k, 0) not in (0, 1)
            for k in ("attached_pic", "default")
        ):
            raise ValueError("FFprobe dispositions must contain integer 0/1 flags.")
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if not videos:
        _issue(issues, "video_stream_not_found", "No eligible video stream found.", "error")
        return {}
    if len(videos) > 1:
        _issue(
            issues, "multiple_video_streams", "Selected default video, then lowest stream index."
        )
    stream = min(videos, key=lambda s: (not s.get("disposition", {}).get("default"), s["index"]))
    result = {"stream_index": stream["index"]}
    for key in ("width", "height"):
        number = _integer(stream.get(key))
        if number is not None:
            result[key] = number
    for source, target in (("codec_name", "codec"), ("pix_fmt", "pixel_format")):
        if isinstance(stream.get(source), str) and stream[source]:
            result[target] = stream[source]
    format_data = raw.get("format", {})
    if not isinstance(format_data, dict):
        raise ValueError("FFprobe format must be an object.")
    if isinstance(format_data.get("format_name"), str):
        result["container"] = format_data["format_name"]

    average, nominal = (_fraction(stream.get(k)) for k in ("avg_frame_rate", "r_frame_rate"))
    rate = average or nominal
    if rate is not None:
        source = "stream.avg_frame_rate" if average else "stream.r_frame_rate"
        result.update(
            frame_rate=float(rate),
            frame_rate_rational=str(rate.numerator) + "/" + str(rate.denominator),
            frame_rate_source=source,
        )
        if average is None:
            _issue(issues, "nominal_frame_rate", "Average rate missing; nominal rate retained.")
        elif nominal is not None and nominal != average:
            _issue(
                issues,
                "possible_variable_frame_rate",
                "Rates differ; constant-rate timing is unverified.",
            )

    ticks = _integer(stream.get("duration_ts"), allow_zero=True)
    base = _fraction(stream.get("time_base"))
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
            "Duration reports differ; exact timestamp duration retained.",
        )
    duration = exact if exact is not None else reported
    source = "stream.duration_ts*time_base" if exact is not None else "stream.duration"
    if duration is None:
        container = _fraction(format_data.get("duration"), allow_zero=True)
        if container is not None:
            result["container_duration_seconds"] = float(container)
            timed = sum(
                s.get("codec_type") != "attachment"
                and not s.get("disposition", {}).get("attached_pic")
                for s in streams
            )
            if timed == 1:
                duration, source = container, "format.duration"
                _issue(
                    issues,
                    "container_duration_fallback",
                    "Single-stream container duration retained.",
                )
            else:
                _issue(
                    issues,
                    "video_duration_ambiguous",
                    "Container duration cannot establish this stream's duration.",
                )
    if duration is not None:
        try:
            number = float(duration)
            if math.isfinite(number) and (number != 0 or duration == 0):
                result.update(
                    duration_seconds=number,
                    duration_rational=f"{duration.numerator}/{duration.denominator}",
                    duration_source=source,
                )
        except OverflowError:
            pass
    for key, target in (("nb_frames", "frame_count"), ("bits_per_raw_sample", "bit_depth")):
        number = _integer(stream.get(key), allow_zero=key == "nb_frames")
        if number is not None:
            result[target] = number
            if key == "nb_frames":
                result["frame_count_source"] = "stream.nb_frames"
    start = _fraction(stream.get("start_time"), allow_zero=True, signed=True)
    if start is not None:
        result["start_time_seconds"] = float(start)
    pixel_format = result.get("pixel_format", "")
    if pixel_format.startswith(("gray", "mono")):
        result["color_mode"] = "grayscale"
    elif pixel_format.startswith(("rgb", "bgr", "gbr")):
        result["color_mode"] = "rgb"
    for key in ("width", "height", "frame_rate", "duration_seconds"):
        if key not in result:
            _issue(issues, "technical_value_unavailable", f"No usable {key} reported.")
    return result


def _decode_probe(output):
    def constant(value):
        raise ValueError(f"Nonfinite JSON constant: {value}")

    def number(token):
        value = float(token)
        if not math.isfinite(value) or (
            value == 0 and any(c in "123456789" for c in token.lower().partition("e")[0])
        ):
            raise ValueError("JSON number is nonfinite or underflows to zero")
        return value

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    raw = json.loads(
        output.decode("utf-8"),
        parse_constant=constant,
        parse_float=number,
        object_pairs_hook=pairs,
    )
    if not isinstance(raw, dict):
        raise ValueError("FFprobe response must be an object.")
    return raw


def extract_metadata(asset, *, ffprobe="ffprobe", timeout=60, max_output_bytes=16_777_216):
    """Extract one imported file; retain failures alongside its evidence."""
    executable = os.fspath(ffprobe)
    _native_tool(executable)
    result = {
        "asset": dict(asset),
        "raw": {},
        "technical": {},
        "issues": [],
        "tool": {"name": "ffprobe", "executable": executable},
    }
    issues, tool = result["issues"], result["tool"]
    try:
        path = Path(asset["path"]).resolve(strict=True)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Input is not a regular file.")
        if any(asset.get(k, v) != v for k, v in file_identity(before).items()):
            _issue(issues, "input_changed", "Input changed after import.", "error")
            return result
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        _issue(issues, "input_unavailable", str(error), "error")
        return result
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
    ]
    command += ["-show_" + section for section in _SECTIONS] + ["-i", str(path)]
    tool["command"] = command
    try:
        code, output, diagnostic = _run_bounded(command, timeout, max_output_bytes)
        if diagnostic:
            tool["stderr"] = diagnostic.decode("utf-8", errors="replace")
            _issue(issues, "ffprobe_diagnostic", "See tool.stderr for probe diagnostics.")
        if code:
            _issue(
                issues,
                "ffprobe_failed",
                f"FFprobe exited with code {code}: " + tool.get("stderr", "")[:2000],
                "error",
            )
        if output.strip():
            try:
                result["raw"] = _decode_probe(output)
                result["technical"] = _technical(result["raw"], issues)
                version = result["raw"].get("program_version", {})
                if isinstance(version, dict):
                    tool["version"] = version.get("version")
            except (UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as error:
                tool["unparsed_stdout_base64"] = base64.b64encode(output).decode("ascii")
                _issue(issues, "invalid_probe_json", str(error), "error")
        elif not code:
            _issue(issues, "invalid_probe_json", "FFprobe returned no JSON.", "error")
    except ProbeFailure as error:
        _issue(issues, error.code, str(error), "error")
    finally:
        try:
            after = path.stat()
            if not stat.S_ISREG(after.st_mode) or file_identity(before) != file_identity(after):
                raise ValueError("Input changed during extraction.")
        except (OSError, ValueError) as error:
            _issue(issues, "input_changed", str(error), "error")
    return result

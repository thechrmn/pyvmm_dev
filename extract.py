"""Stage 2 - read container and stream metadata with FFprobe.

Nothing is decoded and nothing is guessed: a value is reported only when FFprobe
states it, and its source is recorded. FFprobe runs with a deadline, an output cap
and a demuxer whitelist (a text file that merely *names* another file, such as a
concat script, would otherwise make FFprobe describe that other file).
"""

import functools
import json
import os
import re
import shutil
import subprocess
import threading
from fractions import Fraction
from pathlib import Path

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEMUXERS = (
    "3g2 3gp aac asf avi dv flv h264 hevc ivf m4a m4v matroska mj2 mjpeg mov mp3 mp4 "
    "mpeg mpegts mpegvideo mxf nut ogg wav webm yuv4mpegpipe"
).split()
PROBE_ARGUMENTS = (
    *("-v", "warning", "-protocol_whitelist", "file", "-format_whitelist", ",".join(DEMUXERS)),
    *("-print_format", "json", "-show_format", "-show_streams", "-show_chapters", "-show_programs"),
)
_SECTIONS = ("format", "streams", "chapters", "programs")
_WINDOWS = os.name == "nt"
# Elementary streams store no timing: FFprobe fills in a default (25 fps) that is not a measurement.
_UNTIMED = frozenset({"h264", "hevc", "m4v", "mjpeg", "mpegvideo"})
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?(/[0-9]+)?")
_ADDRESS = re.compile(r" @ 0x[0-9a-fA-F]+")  # FFmpeg log prefix; differs on every run


class ProbeError(Exception):
    """FFprobe could not be run to completion; ``code`` becomes the issue code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _run(command, timeout, limit):
    """Run ``command``; return (exit code, stdout, stderr) or raise ProbeError.

    ``timeout`` is seconds for the whole run; ``limit`` caps bytes per output stream.
    The process is always killed and reaped when the caller leaves, interrupts included.
    """
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as error:
        raise ProbeError("ffprobe_unavailable", f"Cannot start FFprobe: {error}") from error
    captured, overflow = {}, threading.Event()

    def drain(name, pipe):
        chunks, size = [], 0
        for chunk in iter(lambda: pipe.read(65536), b""):
            size += len(chunk)
            if size > limit:
                overflow.set()
                process.kill()
                break
            chunks.append(chunk)
        captured[name] = b"".join(chunks)

    readers = [
        threading.Thread(target=drain, args=pair, daemon=True)
        for pair in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=2)  # a wrapper script's orphaned child may hold the pipes
        process.stdout.close()
        process.stderr.close()
    if timed_out:
        raise ProbeError("ffprobe_timeout", f"FFprobe did not finish within {timeout} s.")
    if overflow.is_set():
        raise ProbeError("ffprobe_output_limit", f"FFprobe printed more than {limit} bytes.")
    return process.returncode, captured.get("stdout", b""), captured.get("stderr", b"")


def inspect_ffprobe(ffprobe="ffprobe", *, timeout=10):
    """Resolve and check the FFprobe executable; raise ValueError if it is unusable."""
    executable = shutil.which(os.fspath(ffprobe))
    if executable is None:
        raise ValueError(f"FFprobe executable not found: {ffprobe}")
    executable = str(Path(executable).resolve(strict=True))
    if _WINDOWS and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("Use a native FFprobe executable, not a batch script.")
    try:
        code, output, error = _run([executable, "-version"], timeout, 65536)
    except ProbeError as failure:
        raise ValueError(f"Cannot inspect FFprobe: {failure}") from failure
    lines = output.decode("utf-8", errors="replace").splitlines()
    match = re.match(r"ffprobe version (\S+)", lines[0]) if lines else None
    if code or match is None:
        raise ValueError(
            f"Not a working FFprobe: {executable}. {error.decode('utf-8', 'replace')[:300]}"
        )
    return {
        "executable": executable,
        "version": match[1],
        "banner": lines[0],
        "arguments": list(PROBE_ARGUMENTS),
    }


@functools.lru_cache(maxsize=4)
def _component_depths(ffprobe):
    """{pixel format: bit depth} for formats whose colour components share one depth."""
    command = [ffprobe, "-v", "error", "-show_pixel_formats", "-print_format", "json"]
    try:
        code, output, _ = _run(command, 30, MAX_OUTPUT_BYTES)
        formats = [] if code else json.loads(output.decode("utf-8")).get("pixel_formats", [])
    except (ProbeError, ValueError):
        return {}
    depths = {}
    for pixel_format in formats:
        values = {c.get("bit_depth") for c in pixel_format.get("components", [])}
        if len(values) == 1 and None not in values:
            depths[pixel_format["name"]] = values.pop()
    return depths


def _issue(issues, code, message, severity="warning"):
    issues.append({"severity": severity, "code": code, "message": message, "stage": "extract"})


def _number(value, *, signed=False):
    """Exact Fraction from FFprobe text such as '30000/1001' or '2.002', else None."""
    text = str(value).strip() if value is not None and not isinstance(value, bool) else ""
    if not _NUMBER.fullmatch(text):
        return None
    try:
        number = Fraction(text)
    except ZeroDivisionError:  # FFprobe prints unknown rates as 0/0
        return None
    return number if signed or number >= 0 else None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number.denominator == 1 else None


def _flag(stream, name):
    return bool(stream.get("disposition", {}).get(name))


def _frame_rate(stream, issues):
    average, nominal = _number(stream.get("avg_frame_rate")), _number(stream.get("r_frame_rate"))
    if average and nominal:
        if average > nominal * Fraction(101, 100):
            # r_frame_rate is the base rate all timestamps are multiples of: no average can exceed it.
            _issue(issues, "implausible_average_frame_rate", "Average rate exceeds the base rate; base rate kept.")
            average = None
        elif average != nominal:
            _issue(issues, "possible_variable_frame_rate", "Rates differ; constant rate is unverified.")
    elif nominal:
        _issue(issues, "nominal_frame_rate", "Average rate missing; nominal rate retained.")
    rate = average or nominal
    if not rate:
        return {}
    source = "stream.avg_frame_rate" if average else "stream.r_frame_rate"
    return {"frame_rate": float(rate), "frame_rate_source": source}


def _container_duration(raw, issues):
    """The container's duration, only when it can belong to the video stream (else None)."""
    total = _number(raw.get("format", {}).get("duration"))
    if total is None:
        return None
    timed = [s for s in raw["streams"] if s.get("codec_type") != "attachment" and not _flag(s, "attached_pic")]
    if len(timed) == 1:
        _issue(issues, "container_duration_fallback", "Single-stream container duration kept.")
        return total
    _issue(issues, "video_duration_ambiguous", "Container duration cannot be assigned to video.")
    return None


def _duration(stream, raw, issues):
    ticks, base = _integer(stream.get("duration_ts")), _number(stream.get("time_base"))
    exact = ticks * base if ticks is not None and base else None
    reported = _number(stream.get("duration"))
    if exact is not None and reported is not None and abs(exact - reported) > Fraction(1, 10**6):
        _issue(issues, "duration_mismatch", "Duration reports differ; timestamp duration kept.")
    duration = exact if exact is not None else reported
    source = "stream.duration_ts*time_base" if exact is not None else "stream.duration"
    if duration is None:
        duration, source = _container_duration(raw, issues), "format.duration"
    if duration is None:
        return {}
    return {"duration_seconds": float(duration), "duration_source": source}


def _select_video(raw, issues):
    """The video stream to describe: the default one, else the first. Cover art is not video."""
    videos = [
        s for s in raw.get("streams", []) if s.get("codec_type") == "video" and not _flag(s, "attached_pic")
    ]
    if not videos:
        _issue(issues, "video_stream_not_found", "No video stream found.", "error")
        return None
    if len(videos) > 1:
        _issue(issues, "multiple_video_streams", "Selected the default video stream, else the first.")
    return min(videos, key=lambda s: (not _flag(s, "default"), s["index"]))


def _stream_values(stream, container):
    """Values FFprobe states directly for the stream; an unusable value is left out."""
    result = {"stream_index": stream["index"]}
    for key, source in (("width", "width"), ("height", "height"), ("bit_depth", "bits_per_raw_sample")):
        number = _integer(stream.get(source))
        if number:  # a size or depth of 0 is not usable
            result[key] = number
    if "bit_depth" in result:
        result["bit_depth_source"] = "stream.bits_per_raw_sample"
    frames = _integer(stream.get("nb_frames"))
    if frames is not None:  # 0 frames is a legitimate declared count
        result["frame_count"] = frames
    for key, source in (("codec", "codec_name"), ("pixel_format", "pix_fmt")):
        if isinstance(stream.get(source), str) and stream[source]:
            result[key] = stream[source]
    if isinstance(container.get("format_name"), str):
        result["container"] = container["format_name"]
    start = _number(stream.get("start_time"), signed=True)
    if start is not None:
        result["start_time_seconds"] = float(start)
    return result


def _pixel_facts(result, pixel_depths):
    """Bit depth (when the stream does not declare it) and colour mode, from the pixel format."""
    pixel_format, facts = result.get("pixel_format", ""), {}
    if "bit_depth" not in result and pixel_format in pixel_depths():
        facts["bit_depth"] = pixel_depths()[pixel_format]
        facts["bit_depth_source"] = "ffprobe_pixel_format.component_depth"
    if pixel_format.startswith(("gray", "mono")):
        facts["color_mode"] = "grayscale"
    elif pixel_format.startswith(("rgb", "bgr", "gbr")):
        facts["color_mode"] = "rgb"
    return facts


def _technical(raw, issues, pixel_depths):
    """Normalised values for the selected video stream; unknown values stay absent."""
    stream = _select_video(raw, issues)
    if stream is None:
        return {}
    result = _stream_values(stream, raw.get("format", {}))
    untimed = bool(_UNTIMED.intersection(result.get("container", "").split(",")))
    if untimed:
        _issue(issues, "elementary_stream_timing_unknown", "Raw stream: frame rate and duration are not stored.")
    else:
        result.update(_frame_rate(stream, issues))
        result.update(_duration(stream, raw, issues))
    result.update(_pixel_facts(result, pixel_depths))
    for key in ("width", "height") + (() if untimed else ("frame_rate", "duration_seconds")):
        if key not in result:
            _issue(issues, "technical_value_unavailable", f"No usable {key} reported.")
    return result


def _changed(asset):
    try:
        info = Path(asset["path"]).stat()
    except OSError:
        return True
    return (info.st_size, info.st_mtime_ns) != (asset["size_bytes"], asset["mtime_ns"])


def extract_metadata(asset, *, ffprobe="ffprobe", timeout=60, max_output_bytes=MAX_OUTPUT_BYTES):
    """Probe one asset; never raises for bad media, reports problems as issues.

    Returns ``{"asset", "raw", "technical", "issues"}``. ``raw`` holds FFprobe's
    format, streams, chapters and programs unchanged.
    """
    issues, raw, technical = [], {}, {}
    command = [ffprobe, *PROBE_ARGUMENTS, "-i", asset["path"]]
    try:
        code, output, diagnostic = _run(command, timeout, max_output_bytes)
        text = _ADDRESS.sub("", diagnostic.decode("utf-8", errors="replace")).strip()
        if text:
            _issue(issues, "ffprobe_diagnostic", text[:1000])
        if code:
            _issue(issues, "ffprobe_failed", f"FFprobe exited with status {code}.", "error")
        else:
            parsed = json.loads(output.decode("utf-8"))
            raw = {key: parsed[key] for key in _SECTIONS if key in parsed}
            technical = _technical(raw, issues, lambda: _component_depths(ffprobe))
    except ProbeError as failure:
        _issue(issues, failure.code, str(failure), "error")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raw, technical = {}, {}
        _issue(issues, "invalid_probe_json", f"{type(error).__name__}: {error}", "error")
    if _changed(asset):
        raw, technical = {}, {}
        _issue(issues, "input_changed", "File changed since it was fingerprinted.", "error")
    return {"asset": dict(asset), "raw": raw, "technical": technical, "issues": issues}

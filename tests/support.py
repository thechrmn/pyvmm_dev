"""Shared test fixtures.

Videos are generated with ffmpeg's lavfi test source, so every expectation below comes
from the generation parameters - never from FFprobe's own output. Tests that need a
fixture that this ffmpeg build cannot encode are skipped, never silently passed.
"""

import atexit
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import main  # noqa: E402  (tests run from the project root, so main.py is importable)

importer = importlib.import_module("import")

# One realistic, schema-valid researcher file; README.md must show exactly this example.
VALID_METADATA = {
    "dataset_title": "Open-field tracking, cohort 3",
    "dataset_license": "CC-BY-4.0",
    "hardware_and_acquisition": {
        "hardware": {
            "device_type": "in_house_system",
            "in_house_system_description": "Overhead USB camera above a 40 cm arena",
            "in_house_system_components": ["USB camera", "LED panel"],
            "in_house_system_designer": None,
            "field_of_view_width": 400,
            "field_of_view_height": 400,
            "field_of_view_unit": "mm",
        },
        "acquisition": {"recording_software_name": "VendorRec", "recording_software_version": "2.1"},
    },
}
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ISO_BASE_MEDIA = "mov,mp4,m4a,3gp,3g2,mj2"
README_JSON_MARKER = "<!-- example-metadata -->"


def _source(width, height, rate, frames):
    return ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={rate}", "-frames:v", str(frames)]


def _video(encoder, pix_fmt, width, height, rate, frames, container, depth=8, declared_frames=True, extra=(), **more):
    """(ffmpeg arguments, expected technical values)."""
    args = _source(width, height, rate, frames) + ["-c:v", encoder, "-pix_fmt", pix_fmt, *extra]
    exact_rate = Fraction(str(rate))
    expected = {
        "width": width,
        "height": height,
        "pixel_format": pix_fmt,
        "frame_rate": float(exact_rate),
        "container": container,
        "bit_depth": depth,
        "duration_seconds": float(frames / exact_rate),
    }
    if declared_frames:
        expected["frame_count"] = frames
    expected.update(more)
    return args, expected


def _raw(encoder, muxer, container, codec, pix_fmt, extra=()):
    """A 30 fps elementary stream. Muxer "mpeg2video" is written by extension, the rest by name."""
    forced = [] if muxer == "mpeg2video" else ["-f", muxer]
    args = _source(64, 48, 30, 60) + ["-c:v", encoder, "-pix_fmt", pix_fmt, *extra, *forced]
    return args, {"width": 64, "height": 48, "codec": codec, "container": container,
                  "pixel_format": pix_fmt, "bit_depth": 8}


# name -> (ffmpeg args, expected technical values, expected BeStMeta codec, expected BeStMeta container)
VIDEOS = {
    "h264_mp4.mp4": (*_video("libx264", "yuv420p", 320, 240, 30, 60, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4"),
    "h265_mp4.mp4": (
        *_video("libx265", "yuv420p", 320, 240, 25, 40, ISO_BASE_MEDIA, codec="hevc",
                extra=["-tag:v", "hvc1", "-x265-params", "log-level=none"]),
        "h265", "mp4",
    ),
    "h264_mov.mov": (*_video("libx264", "yuv420p", 320, 240, 30, 30, ISO_BASE_MEDIA, codec="h264"), "h264", "mov"),
    "av1_mp4.mp4": (*_video("libsvtav1", "yuv420p", 320, 240, 24, 24, ISO_BASE_MEDIA, codec="av1"), "av1", "mp4"),
    "prores_mov.mov": (
        *_video("prores_ks", "yuv422p10le", 320, 240, 25, 25, ISO_BASE_MEDIA, depth=10, codec="prores"),
        "other", "mov",
    ),
    "mjpeg_avi.avi": (
        *_video("mjpeg", "yuvj420p", 320, 240, 30, 45, "avi", codec="mjpeg", extra=["-q:v", "3"]),
        "mjpeg", "avi",
    ),
    "mpeg4_avi.avi": (*_video("mpeg4", "yuv420p", 320, 240, 30, 30, "avi", codec="mpeg4"), "mpeg4", "avi"),
    "raw_rgb_avi.avi": (
        *_video("rawvideo", "bgr24", 160, 120, 20, 20, "avi", codec="rawvideo", color_mode="rgb"), "raw", "avi",
    ),
    "ffv1_gray_mkv.mkv": (
        *_video("ffv1", "gray", 320, 240, 30, 30, "matroska,webm", declared_frames=False, codec="ffv1",
                color_mode="grayscale"),
        "ffv1", "other",
    ),
    "ffv1_gray10_mkv.mkv": (
        *_video("ffv1", "gray10le", 320, 240, 30, 30, "matroska,webm", depth=10, declared_frames=False,
                codec="ffv1", color_mode="grayscale"),
        "ffv1", "other",
    ),
    "ffv1_gray_avi.avi": (
        *_video("ffv1", "gray", 320, 240, 30, 30, "avi", codec="ffv1", color_mode="grayscale"), "ffv1", "avi",
    ),
    "vp9_webm.webm": (
        *_video("libvpx-vp9", "yuv420p", 320, 240, 30, 30, "matroska,webm", declared_frames=False, codec="vp9",
                extra=["-b:v", "200k"]),
        "other", "other",
    ),
    "mpeg2_ts.ts": (
        *_video("mpeg2video", "yuv420p", 320, 240, 25, 25, "mpegts", declared_frames=False, codec="mpeg2video"),
        "other", "other",
    ),
    "theora_ogv.ogv": (
        *_video("libtheora", "yuv420p", 320, 240, 25, 25, "ogg", declared_frames=False, codec="theora"),
        "other", "other",
    ),
    "ntsc_2997.mp4": (
        *_video("libx264", "yuv420p", 320, 240, "30000/1001", 60, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4",
    ),
    "highspeed_1000fps.avi": (
        *_video("mjpeg", "yuvj420p", 64, 48, 1000, 500, "avi", codec="mjpeg"), "mjpeg", "avi",
    ),
    "odd_dims.mp4": (*_video("libx264", "yuv420p", 318, 238, 30, 15, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4"),
    "single_frame.mp4": (*_video("libx264", "yuv420p", 320, 240, 30, 1, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4"),
    "tagged.mp4": (
        *_video("libx264", "yuv420p", 320, 240, 30, 30, ISO_BASE_MEDIA, codec="h264",
                extra=["-movflags", "+use_metadata_tags", "-metadata", "title=Assay 7",
                       "-metadata", "camera_model=Basler acA1300"]),
        "h264", "mp4",
    ),
    "ünïcödé 视频 (1).mp4": (
        *_video("libx264", "yuv420p", 160, 120, 30, 10, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4",
    ),
    "-leading_dash.mp4": (
        *_video("libx264", "yuv420p", 160, 120, 30, 10, ISO_BASE_MEDIA, codec="h264"), "h264", "mp4",
    ),
    "camera.dv": (
        _source(720, 576, 25, 5) + ["-c:v", "dvvideo", "-pix_fmt", "yuv420p"],
        {"width": 720, "height": 576, "codec": "dvvideo", "container": "dv", "pixel_format": "yuv420p",
         "bit_depth": 8, "frame_rate": 25.0, "duration_seconds": 0.2},
        "other", "other",
    ),
    "frames.y4m": (
        _source(64, 48, 10, 5) + ["-f", "yuv4mpegpipe"],
        {"width": 64, "height": 48, "codec": "rawvideo", "container": "yuv4mpegpipe", "pixel_format": "yuv420p",
         "bit_depth": 8, "frame_rate": 10.0, "duration_seconds": 0.5},
        "raw", "other",
    ),
    "stream.ivf": (
        _source(64, 48, 10, 5) + ["-c:v", "libvpx", "-f", "ivf"],
        {"width": 64, "height": 48, "codec": "vp8", "container": "ivf", "pixel_format": "yuv420p",
         "bit_depth": 8, "frame_rate": 10.0, "duration_seconds": 0.5, "frame_count": 5},
        "other", "other",
    ),
    # Raw elementary streams store no timing: FFprobe assumes 25 fps, so none may be reported.
    "raw_stream.h264": (*_raw("libx264", "h264", "h264", "h264", "yuv420p"), "h264", "other"),
    "raw_stream.mjpeg": (*_raw("mjpeg", "mjpeg", "mjpeg", "mjpeg", "yuvj420p"), "mjpeg", "other"),
    "raw_stream.hevc": (
        *_raw("libx265", "hevc", "hevc", "hevc", "yuv420p", extra=["-x265-params", "log-level=none"]), "h265", "other",
    ),
    "raw_stream.m4v": (*_raw("mpeg4", "m4v", "m4v", "mpeg4", "yuv420p"), "mpeg4", "other"),
    "raw_stream.m2v": (*_raw("mpeg2video", "mpeg2video", "mpegvideo", "mpeg2video", "yuv420p"), "other", "other"),
}

# Not videos: FFprobe must refuse them, and nothing may be learned from other files.
UNREADABLE = (
    "truncated.mp4", "zero_bytes.mp4", "garbage.mp4", "text_named.mp4",
    "concat_redirect.mp4", "hls_redirect.mp4", "evil_ref.mp4", "evil.ffconcat", "evil.m3u8",
)


class Corpus:
    def __init__(self, root, skipped):
        self.root = root
        self.skipped = skipped

    def path(self, name):
        if name in self.skipped:
            raise unittest.SkipTest(f"fixture {name} could not be encoded here: {self.skipped[name][:150]}")
        return self.root / name


_cache = {}


def _build():
    root = Path(tempfile.mkdtemp(prefix="pyvmm-corpus-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    skipped = {}

    def encode(args, output):
        command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        return result.returncode == 0, result.stderr.strip()[-300:]

    for name, (args, _expected, _codec, _container) in VIDEOS.items():
        ok, error = encode(args, root / name)
        if not ok:
            skipped[name] = error
    if skipped:
        print(f"note: {len(skipped)} media fixtures could not be built with this FFmpeg and are skipped: "
              + ", ".join(sorted(skipped)), file=sys.stderr)
    if "h264_mp4.mp4" in skipped:
        message = f"this FFmpeg cannot encode H.264, which the media fixtures need: {skipped['h264_mp4.mp4']}"
        raise AssertionError(message) if os.environ.get("CI") else unittest.SkipTest(message)
    base = root / "h264_mp4.mp4"
    data = base.read_bytes()
    (root / "truncated.mp4").write_bytes(data[: len(data) // 3])
    (root / "zero_bytes.mp4").write_bytes(b"")
    (root / "garbage.mp4").write_bytes(bytes(range(256)) * 16)
    (root / "text_named.mp4").write_text("this is not a video\n")
    (root / "concat_redirect.mp4").write_text("ffconcat version 1.0\nfile 'h264_mp4.mp4'\n")
    (root / "hls_redirect.mp4").write_text("#EXTM3U\n#EXTINF:2,\nh264_mp4.mp4\n#EXT-X-ENDLIST\n")
    (root / "evil_ref.mp4").write_text("ffconcat version 1.0\nfile '/etc/hostname'\n")
    (root / "evil.ffconcat").write_text("ffconcat version 1.0\nfile '/etc/passwd'\n")
    (root / "evil.m3u8").write_text("#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n")
    shutil.copy(base, root / "duplicate_of_h264.mp4")
    shutil.copy(base, root / "video_with_txt_extension.txt")
    (root / "sub").mkdir()
    if "mjpeg_avi.avi" not in skipped:
        shutil.copy(root / "mjpeg_avi.avi", root / "sub" / "nested.avi")
    return Corpus(root, skipped)


def corpus():
    """Build the corpus once per test process; skip (or, on CI, fail) the caller if tools are missing."""
    if FFMPEG is None or FFPROBE is None:
        raise unittest.SkipTest("ffmpeg and ffprobe are required to build media fixtures")
    if "corpus" not in _cache:
        try:
            _cache["corpus"] = _build()
        except (unittest.SkipTest, AssertionError) as problem:
            _cache["corpus"] = problem
    result = _cache["corpus"]
    if isinstance(result, Exception):
        raise result
    return result


def require(corp, *names):
    for name in names:
        if name in corp.skipped:
            raise unittest.SkipTest(f"fixture {name} could not be encoded here: {corp.skipped[name]}")


def run_main(*argv):
    """Run main.main in-process; return (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = main.main([str(a) for a in argv])
        except SystemExit as stop:  # argparse usage errors
            status = stop.code
    return status, out.getvalue(), err.getvalue()


def read_report(folder):
    return json.loads((Path(folder) / "metadata.json").read_text(encoding="utf-8"))


def records_by_name(report):
    return {r["extraction"]["asset"]["relative_path"]: r for r in report["records"]}


class TempDirCase(unittest.TestCase):
    """Each test gets a fresh scratch directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pyvmm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def out(self, name="out"):
        return self.tmp / name


def fake_ffprobe(directory, body):
    """A POSIX executable that replaces FFprobe; returns its path (None on Windows)."""
    if os.name == "nt":
        return None
    path = Path(directory) / "fake-ffprobe"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)

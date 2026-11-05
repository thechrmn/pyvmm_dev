import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import extract
from tests import support
from tests.support import TempDirCase, fake_ffprobe, importer


def stream(**changes):
    base = {
        "index": 0, "codec_type": "video", "codec_name": "h264", "width": 320, "height": 240,
        "pix_fmt": "yuv420p", "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
        "time_base": "1/15360", "duration_ts": 30720, "duration": "2.000000", "nb_frames": "60",
        "bits_per_raw_sample": "8", "start_time": "0.000000",
        "disposition": {"default": 1, "attached_pic": 0},
    }
    base.update(changes)
    return base


def derive(*streams, container=None, depths=None):
    issues = []
    raw = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", **(container or {})}, "streams": list(streams)}
    return extract._technical(raw, issues, lambda: depths or {}), [(i["severity"], i["code"]) for i in issues]


def codes(issues):
    return [code for _, code in issues]


class NumberTests(unittest.TestCase):
    def test_exact_values(self):
        for text, want in (("30000/1001", Fraction(30000, 1001)), ("2.002", Fraction(2002, 1000)),
                           (60, 60), ("0", 0), (" 7 ", 7), ("-0", 0)):
            with self.subTest(text):
                self.assertEqual(extract._number(text), want)

    def test_everything_else_is_rejected(self):
        for text in ("", "abc", "1e5", "0/0", "1/0", "-5", "5/-2", "٣٠", "1_000", "inf", "nan", "+5",
                     "1e999999999", True, None, [1], {}):
            with self.subTest(repr(text)):
                self.assertIsNone(extract._number(text))

    def test_signed_values_may_be_negative(self):
        self.assertEqual(extract._number("-0.5", signed=True), Fraction(-1, 2))

    def test_integer_requires_a_whole_number(self):
        self.assertEqual(extract._integer("60"), 60)
        self.assertEqual(extract._integer("2.0"), 2)
        for text in ("2.5", "-1", "x", None):
            self.assertIsNone(extract._integer(text))


class TechnicalTests(unittest.TestCase):
    def test_complete_constant_rate_stream(self):
        technical, issues = derive(stream())
        self.assertEqual(
            technical,
            {
                "stream_index": 0, "width": 320, "height": 240, "frame_count": 60, "bit_depth": 8,
                "bit_depth_source": "stream.bits_per_raw_sample", "codec": "h264", "pixel_format": "yuv420p",
                "container": "mov,mp4,m4a,3gp,3g2,mj2", "frame_rate": 30.0,
                "frame_rate_source": "stream.avg_frame_rate", "duration_seconds": 2.0,
                "duration_source": "stream.duration_ts*time_base", "start_time_seconds": 0.0,
            },
        )
        self.assertEqual(issues, [])

    def test_ntsc_rate_is_exact_not_rounded(self):
        technical, _ = derive(stream(avg_frame_rate="30000/1001", r_frame_rate="30000/1001"))
        self.assertEqual(technical["frame_rate"], 30000 / 1001)

    def test_missing_average_rate_falls_back_to_nominal_with_a_warning(self):
        technical, issues = derive(stream(avg_frame_rate="0/0"))
        self.assertEqual((technical["frame_rate"], technical["frame_rate_source"]), (30.0, "stream.r_frame_rate"))
        self.assertEqual(issues, [("warning", "nominal_frame_rate")])

    def test_differing_rates_flag_possible_variable_rate_and_keep_the_average(self):
        technical, issues = derive(stream(avg_frame_rate="18/1", r_frame_rate="30/1"))
        self.assertEqual(technical["frame_rate"], 18.0)
        self.assertEqual(codes(issues), ["possible_variable_frame_rate"])

    def test_an_average_above_the_base_rate_is_impossible_and_not_trusted(self):
        technical, issues = derive(stream(avg_frame_rate="60000/1", r_frame_rate="25/1"))
        self.assertEqual((technical["frame_rate"], technical["frame_rate_source"]), (25.0, "stream.r_frame_rate"))
        self.assertEqual(codes(issues), ["implausible_average_frame_rate"])

    def test_small_rounding_above_the_base_rate_is_tolerated(self):
        technical, issues = derive(stream(avg_frame_rate="3025/100", r_frame_rate="30/1"))
        self.assertEqual(technical["frame_rate_source"], "stream.avg_frame_rate")
        self.assertEqual(codes(issues), ["possible_variable_frame_rate"])
        technical, issues = derive(stream(avg_frame_rate="31/1", r_frame_rate="30/1"))
        self.assertEqual(codes(issues), ["implausible_average_frame_rate"])

    def test_raw_elementary_streams_report_no_timing_at_all(self):
        for container in ("h264", "hevc", "m4v", "mjpeg", "mpegvideo"):
            with self.subTest(container):
                technical, issues = derive(stream(avg_frame_rate="25/1", r_frame_rate="30/1"),
                                           container={"format_name": container})
                for key in ("frame_rate", "frame_rate_source", "duration_seconds", "duration_source"):
                    self.assertNotIn(key, technical)
                self.assertEqual(technical["width"], 320)
                self.assertEqual(codes(issues), ["elementary_stream_timing_unknown"])

    def test_timing_is_kept_for_containers_that_store_it(self):
        for container in ("mpegts", "avi", "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "ogg"):
            with self.subTest(container):
                technical, _ = derive(stream(), container={"format_name": container})
                self.assertEqual(technical["frame_rate"], 30.0)

    def test_untimed_stream_still_reports_missing_dimensions(self):
        technical, issues = derive(stream(width=0), container={"format_name": "h264"})
        self.assertEqual(codes(issues), ["elementary_stream_timing_unknown", "technical_value_unavailable"])

    def test_no_rate_at_all_is_reported_not_invented(self):
        technical, issues = derive(stream(avg_frame_rate="0/0", r_frame_rate="0/0"))
        self.assertNotIn("frame_rate", technical)
        self.assertIn("technical_value_unavailable", codes(issues))

    def test_timestamp_duration_is_exact_and_disagreement_is_flagged(self):
        technical, issues = derive(stream(duration_ts=45000, time_base="1/90000", duration="9.000000"))
        self.assertEqual(technical["duration_seconds"], 0.5)
        self.assertEqual(codes(issues), ["duration_mismatch"])

    def test_stream_duration_text_is_used_without_timestamps(self):
        technical, issues = derive(stream(duration_ts=None, time_base=None, duration="2.5"))
        self.assertEqual((technical["duration_seconds"], technical["duration_source"]), (2.5, "stream.duration"))
        self.assertEqual(issues, [])

    def test_single_stream_container_duration_is_a_flagged_fallback(self):
        technical, issues = derive(
            stream(duration_ts=None, time_base=None, duration=None), container={"duration": "3.25"}
        )
        self.assertEqual((technical["duration_seconds"], technical["duration_source"]), (3.25, "format.duration"))
        self.assertEqual(codes(issues), ["container_duration_fallback"])

    def test_container_duration_is_not_assigned_to_video_when_other_streams_exist(self):
        audio = {"index": 1, "codec_type": "audio"}
        technical, issues = derive(
            stream(duration_ts=None, time_base=None, duration=None), audio, container={"duration": "3.25"}
        )
        self.assertNotIn("duration_seconds", technical)
        self.assertEqual(codes(issues), ["video_duration_ambiguous", "technical_value_unavailable"])

    def test_cover_art_is_not_a_video_stream(self):
        cover = stream(disposition={"default": 0, "attached_pic": 1})
        technical, issues = derive(cover, {"index": 1, "codec_type": "audio"})
        self.assertEqual((technical, issues), ({}, [("error", "video_stream_not_found")]))

    def test_default_stream_wins_then_lowest_index(self):
        first = stream(index=0, width=100, disposition={"default": 0})
        second = stream(index=1, width=200, disposition={"default": 1})
        third = stream(index=2, width=300, disposition={"default": 0})
        technical, issues = derive(first, second, third)
        self.assertEqual((technical["stream_index"], technical["width"]), (1, 200))
        self.assertEqual(codes(issues), ["multiple_video_streams"])
        technical, _ = derive(third, first)
        self.assertEqual(technical["stream_index"], 0)

    def test_zero_dimensions_are_unusable(self):
        technical, issues = derive(stream(width=0, height="x"))
        self.assertNotIn("width", technical)
        self.assertNotIn("height", technical)
        self.assertEqual(codes(issues), ["technical_value_unavailable"] * 2)

    def test_frame_count_may_be_zero_but_never_junk_or_invented(self):
        self.assertEqual(derive(stream(nb_frames="0"))[0]["frame_count"], 0)
        self.assertNotIn("frame_count", derive(stream(nb_frames="abc"))[0])
        self.assertNotIn("frame_count", derive(stream(nb_frames=None))[0])
        missing = stream()
        del missing["nb_frames"]
        self.assertNotIn("frame_count", derive(missing)[0])

    def test_colour_mode_from_pixel_format(self):
        table = {
            "gray": "grayscale", "gray10le": "grayscale", "monob": "grayscale", "rgb24": "rgb", "bgr24": "rgb",
            "gbrp": "rgb", "rgba": "rgb", "yuv420p": None, "pal8": None, "nv12": None,
        }
        for pixel_format, want in table.items():
            with self.subTest(pixel_format):
                technical, _ = derive(stream(pix_fmt=pixel_format))
                self.assertEqual(technical.get("color_mode"), want)

    def test_declared_bit_depth_wins_and_skips_the_pixel_format_table(self):
        lookups = []
        technical, _ = derive(stream(bits_per_raw_sample="10", pix_fmt="gray16le"), depths=None)
        self.assertEqual((technical["bit_depth"], technical["bit_depth_source"]), (10, "stream.bits_per_raw_sample"))
        issues = []
        raw = {"streams": [stream(bits_per_raw_sample="10")]}
        extract._technical(raw, issues, lambda: lookups.append(1) or {})
        self.assertEqual(lookups, [])

    def test_bit_depth_falls_back_to_the_pixel_format_component_depth(self):
        undeclared = stream(bits_per_raw_sample=None, pix_fmt="gray")
        technical, _ = derive(undeclared, depths={"gray": 8})
        self.assertEqual(
            (technical["bit_depth"], technical["bit_depth_source"]),
            (8, "ffprobe_pixel_format.component_depth"),
        )
        technical, _ = derive(stream(bits_per_raw_sample=None, pix_fmt="rgb565le"), depths={"gray": 8})
        self.assertNotIn("bit_depth", technical)

    def test_negative_start_time_is_kept(self):
        self.assertEqual(derive(stream(start_time="-0.5"))[0]["start_time_seconds"], -0.5)

    def test_missing_container_name_is_absent(self):
        issues = []
        raw = {"format": {}, "streams": [stream()]}
        self.assertNotIn("container", extract._technical(raw, issues, dict))


class ExtractMetadataTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "v.mp4"
        self.path.write_bytes(b"x" * 100)
        self.asset = importer.fingerprint_file(self.path, "v.mp4")

    def probe(self, output=b"", code=0, diagnostic=b"", **options):
        with mock.patch.object(extract, "_run", return_value=(code, output, diagnostic)) as run:
            result = extract.extract_metadata(self.asset, ffprobe="FFPROBE", **options)
        self.run_mock = run
        return result

    def issue_codes(self, result):
        return [(i["severity"], i["code"]) for i in result["issues"]]

    def test_success_keeps_only_the_documented_raw_sections(self):
        payload = {"format": {"format_name": "avi"}, "streams": [stream()], "chapters": [], "programs": [],
                   "error": {"string": "ignored"}, "extra": 1}
        result = self.probe(json.dumps(payload).encode())
        self.assertEqual(sorted(result["raw"]), ["chapters", "format", "programs", "streams"])
        self.assertEqual(result["technical"]["width"], 320)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["asset"], self.asset)
        self.assertIsNot(result["asset"], self.asset)

    def test_command_is_hardened_and_never_goes_through_a_shell(self):
        self.probe(json.dumps({"streams": [stream()]}).encode(), timeout=7, max_output_bytes=99)
        command, timeout, limit = self.run_mock.call_args.args
        self.assertEqual(command, ["FFPROBE", *extract.PROBE_ARGUMENTS, "-i", str(self.path)])
        self.assertEqual((timeout, limit), (7, 99))
        arguments = dict(zip(command, command[1:], strict=False))
        self.assertEqual(arguments["-protocol_whitelist"], "file")
        self.assertEqual(set(arguments["-format_whitelist"].split(",")), set(extract.DEMUXERS))
        self.assertNotIn("concat", extract.DEMUXERS)
        self.assertNotIn("hls", extract.DEMUXERS)

    def test_failure_status_is_an_error_with_the_diagnostic_kept(self):
        result = self.probe(code=1, diagnostic=b"moov atom not found\n")
        self.assertEqual(self.issue_codes(result), [("warning", "ffprobe_diagnostic"), ("error", "ffprobe_failed")])
        self.assertIn("moov atom", result["issues"][0]["message"])
        self.assertEqual((result["raw"], result["technical"]), ({}, {}))

    def test_diagnostics_carry_no_run_specific_memory_addresses(self):
        text = b"[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55e7ad1351c0] moov atom not found\n[h264 @ 0x7f00deadbeef] x\n"
        result = self.probe(code=1, diagnostic=text)
        self.assertEqual(result["issues"][0]["message"], "[mov,mp4,m4a,3gp,3g2,mj2] moov atom not found\n[h264] x")

    def test_diagnostic_on_success_is_a_truncated_warning(self):
        result = self.probe(json.dumps({"streams": [stream()]}).encode(), diagnostic=b"w" * 5000)
        self.assertEqual(self.issue_codes(result), [("warning", "ffprobe_diagnostic")])
        self.assertEqual(len(result["issues"][0]["message"]), 1000)

    def test_probe_failures_become_issues_with_their_own_codes(self):
        for code in ("ffprobe_timeout", "ffprobe_output_limit", "ffprobe_unavailable"):
            with self.subTest(code):
                with mock.patch.object(extract, "_run", side_effect=extract.ProbeError(code, "boom")):
                    result = extract.extract_metadata(self.asset)
                self.assertEqual(self.issue_codes(result), [("error", code)])

    def test_unusable_output_is_reported_not_raised(self):
        for output in (b"not json", b"\xff\xfe", b'{"streams": 5}', b'{"streams": [1]}',
                       b'{"streams": [{"codec_type": "video"}]}'):
            with self.subTest(output):
                result = self.probe(output)
                self.assertEqual(self.issue_codes(result), [("error", "invalid_probe_json")])
                self.assertEqual((result["raw"], result["technical"]), ({}, {}))

    def test_no_streams_is_an_error(self):
        result = self.probe(b"{}")
        self.assertEqual(self.issue_codes(result), [("error", "video_stream_not_found")])

    def test_a_file_that_changed_is_not_trusted(self):
        with mock.patch.object(extract, "_run") as run:
            def modify_during_probe(*_):
                self.path.write_bytes(b"y" * 5)
                return 0, json.dumps({"streams": [stream()]}).encode(), b""

            run.side_effect = modify_during_probe
            result = extract.extract_metadata(self.asset)
        self.assertEqual(self.issue_codes(result), [("error", "input_changed")])
        self.assertEqual((result["raw"], result["technical"]), ({}, {}))

    def test_a_vanished_file_counts_as_changed(self):
        with mock.patch.object(extract, "_run", return_value=(0, b'{"streams": []}', b"")):
            self.path.unlink()
            result = extract.extract_metadata(self.asset)
        self.assertIn(("error", "input_changed"), self.issue_codes(result))


class RunTests(unittest.TestCase):
    def python(self, code):
        return [sys.executable, "-c", code]

    def test_captures_both_streams_and_the_exit_status(self):
        code = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
        self.assertEqual(extract._run(self.python(code), 30, 1000), (3, b"out", b"err"))

    def test_output_below_the_limit_is_complete(self):
        code = "import sys; sys.stdout.write('x' * 3_000_000)"
        status, out, _ = extract._run(self.python(code), 30, 16 * 1024 * 1024)
        self.assertEqual((status, len(out)), (0, 3_000_000))

    def test_missing_executable(self):
        with self.assertRaises(extract.ProbeError) as caught:
            extract._run(["/nonexistent/ffprobe"], 5, 100)
        self.assertEqual(caught.exception.code, "ffprobe_unavailable")

    def test_output_flood_is_cut_off_on_either_stream(self):
        for stream_name in ("stdout", "stderr"):
            with self.subTest(stream_name):
                code = f"import sys\nwhile True: sys.{stream_name}.write('x' * 65536); sys.{stream_name}.flush()"
                started = time.monotonic()
                with self.assertRaises(extract.ProbeError) as caught:
                    extract._run(self.python(code), 60, 1_000_000)
                self.assertEqual(caught.exception.code, "ffprobe_output_limit")
                self.assertLess(time.monotonic() - started, 20)

    def pid_file_script(self, pid_path):
        return f"import os, time\nopen({str(pid_path)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @unittest.skipIf(os.name == "nt", "process probing differs on Windows")
    def test_hung_process_is_killed_at_the_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_path = Path(folder) / "pid"
            started = time.monotonic()
            with self.assertRaises(extract.ProbeError) as caught:
                extract._run(self.python(self.pid_file_script(pid_path)), 1, 1000)
            self.assertEqual(caught.exception.code, "ffprobe_timeout")
            self.assertLess(time.monotonic() - started, 15)
            self.assertFalse(self.alive(int(pid_path.read_text())))

    @unittest.skipIf(os.name == "nt", "process probing differs on Windows")
    def test_interrupt_kills_the_child_and_propagates(self):
        real_wait, calls = subprocess.Popen.wait, []

        def interrupting(self_, timeout=None):
            if not calls:
                calls.append(1)
                time.sleep(0.5)  # let the child record its pid
                raise KeyboardInterrupt
            return real_wait(self_, timeout)

        with tempfile.TemporaryDirectory() as folder:
            pid_path = Path(folder) / "pid"
            with mock.patch.object(subprocess.Popen, "wait", interrupting):
                with self.assertRaises(KeyboardInterrupt):
                    extract._run(self.python(self.pid_file_script(pid_path)), 60, 1000)
            self.assertFalse(self.alive(int(pid_path.read_text())))


class FakeProbeTests(TempDirCase):
    """End-to-end behaviour against misbehaving ffprobe replacements (POSIX only)."""

    def extract_with(self, body, **options):
        probe = fake_ffprobe(self.tmp, body)
        if probe is None:
            self.skipTest("needs a POSIX shell")
        path = self.tmp / "v.mp4"
        path.write_bytes(b"abc")
        asset = importer.fingerprint_file(path, "v.mp4")
        return extract.extract_metadata(asset, ffprobe=probe, **options)

    def only_issue(self, result):
        return [(i["severity"], i["code"]) for i in result["issues"] if i["code"] != "ffprobe_diagnostic"]

    def test_hang(self):
        started = time.monotonic()
        result = self.extract_with("exec sleep 60", timeout=1)
        self.assertEqual(self.only_issue(result), [("error", "ffprobe_timeout")])
        self.assertLess(time.monotonic() - started, 15)

    def test_flood(self):
        result = self.extract_with("exec yes", max_output_bytes=100_000)
        self.assertEqual(self.only_issue(result), [("error", "ffprobe_output_limit")])

    def test_crash_by_signal(self):
        result = self.extract_with("kill -SEGV $$")
        self.assertEqual(self.only_issue(result), [("error", "ffprobe_failed")])
        self.assertIn("-11", result["issues"][-1]["message"])

    def test_garbage_on_success(self):
        result = self.extract_with("echo hello")
        self.assertEqual(self.only_issue(result), [("error", "invalid_probe_json")])


class InspectTests(TempDirCase):
    def test_real_ffprobe(self):
        if support.FFPROBE is None:
            self.skipTest("ffprobe not installed")
        info = extract.inspect_ffprobe()
        self.assertTrue(Path(info["executable"]).is_absolute())
        self.assertRegex(info["version"], r"^\S+$")
        self.assertTrue(info["banner"].startswith("ffprobe version "))
        self.assertEqual(info["arguments"], list(extract.PROBE_ARGUMENTS))

    def test_missing_executable(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            extract.inspect_ffprobe("definitely-not-installed-ffprobe")

    def test_other_programs_are_not_accepted_as_ffprobe(self):
        with self.assertRaisesRegex(ValueError, "Not a working FFprobe"):
            extract.inspect_ffprobe(sys.executable)

    def test_failing_executable_is_rejected(self):
        probe = fake_ffprobe(self.tmp, "echo 'ffprobe version 9' ; exit 1")
        if probe is None:
            self.skipTest("needs a POSIX shell")
        with self.assertRaisesRegex(ValueError, "Not a working FFprobe"):
            extract.inspect_ffprobe(probe)

    def test_an_executable_that_hangs_on_version_is_rejected(self):
        probe = fake_ffprobe(self.tmp, "exec sleep 60")
        if probe is None:
            self.skipTest("needs a POSIX shell")
        with self.assertRaisesRegex(ValueError, "Cannot inspect FFprobe"):
            extract.inspect_ffprobe(probe, timeout=1)

    def test_batch_scripts_are_refused_on_windows(self):
        script = self.tmp / "ffprobe.bat"
        script.write_text("@echo off\n")
        with mock.patch.object(extract, "_WINDOWS", True), mock.patch.object(extract.shutil, "which", return_value=str(script)):
            with self.assertRaisesRegex(ValueError, "native FFprobe"):
                extract.inspect_ffprobe("ffprobe")

    def test_pixel_format_table_is_uniform_depth_only_and_cached(self):
        if support.FFPROBE is None:
            self.skipTest("ffprobe not installed")
        extract._component_depths.cache_clear()
        depths = extract._component_depths(support.FFPROBE)
        self.assertEqual((depths["gray"], depths["gray10le"], depths["yuv420p"], depths["bgr24"]), (8, 10, 8, 8))
        self.assertNotIn("rgb565le", depths)  # components differ: 5, 6, 5
        extract._component_depths(support.FFPROBE)
        self.assertEqual(extract._component_depths.cache_info().hits, 1)

    def test_pixel_format_table_degrades_to_empty(self):
        extract._component_depths.cache_clear()
        self.assertEqual(extract._component_depths("/nonexistent/ffprobe"), {})
        extract._component_depths.cache_clear()


class OracleTests(TempDirCase):
    """Real FFprobe against media whose properties are known from how it was generated."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = support.corpus()
        cls.ffprobe = extract.inspect_ffprobe()["executable"]

    def derived(self, name):
        return self.tmp / name

    def extract(self, name):
        support.require(self.corpus, name)
        asset = importer.fingerprint_file(self.corpus.path(name), name)
        return extract.extract_metadata(asset, ffprobe=self.ffprobe)

    def test_every_video_matches_its_generation_parameters(self):
        for name, (_args, expected, _codec, _container) in support.VIDEOS.items():
            with self.subTest(name):
                result = self.extract(name)
                technical = result["technical"]
                self.assertEqual([i for i in result["issues"] if i["severity"] == "error"], [])
                for key, want in expected.items():
                    if key == "duration_seconds":
                        frame = 1 / expected.get("frame_rate", 25)
                        self.assertLessEqual(abs(technical[key] - want), 2 * frame, key)
                    elif key == "frame_rate":
                        self.assertAlmostEqual(technical[key] / want, 1, places=3)
                    else:
                        self.assertEqual(technical.get(key), want, key)
                for key in ("frame_count", "color_mode", "bit_depth", "frame_rate", "duration_seconds"):
                    if key not in expected:
                        self.assertNotIn(key, technical, f"{key} must not be invented")

    def test_unreadable_files_are_refused_and_reveal_nothing(self):
        for name in support.UNREADABLE:
            with self.subTest(name):
                result = self.extract(name)
                self.assertIn(("error", "ffprobe_failed"), [(i["severity"], i["code"]) for i in result["issues"]])
                self.assertEqual((result["raw"], result["technical"]), ({}, {}))

    def test_a_text_file_naming_another_video_is_not_described_as_that_video(self):
        for name in ("concat_redirect.mp4", "hls_redirect.mp4"):
            with self.subTest(name):
                self.assertEqual(self.extract(name)["technical"], {})

    def test_rotation_keeps_stored_dimensions(self):
        support.require(self.corpus, "h264_mp4.mp4")
        rotated = self.derived("rotated_test.mp4")
        subprocess.run(
            [support.FFMPEG, "-v", "error", "-y", "-display_rotation", "90", "-i",
             str(self.corpus.path("h264_mp4.mp4")), "-c", "copy", str(rotated)], check=True)
        asset = importer.fingerprint_file(rotated, "rotated_test.mp4")
        technical = extract.extract_metadata(asset, ffprobe=self.ffprobe)["technical"]
        self.assertEqual((technical["width"], technical["height"]), (320, 240))

    def test_variable_rate_is_flagged_and_frames_are_the_declared_count(self):
        support.require(self.corpus, "h264_mp4.mp4")
        vfr = self.derived("vfr_test.mp4")
        subprocess.run(
            [support.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30",
             "-frames:v", "90", "-vf", "select='lt(mod(n,5),3)'", "-fps_mode", "vfr", str(vfr)], check=True)
        result = extract.extract_metadata(importer.fingerprint_file(vfr, "vfr_test.mp4"), ffprobe=self.ffprobe)
        self.assertIn("possible_variable_frame_rate", [i["code"] for i in result["issues"]])
        self.assertEqual(result["technical"]["frame_count"], 90)

    def test_real_deadline_and_output_limit(self):
        for options, code in (({"timeout": 1e-9}, "ffprobe_timeout"), ({"max_output_bytes": 50}, "ffprobe_output_limit")):
            with self.subTest(code):
                support.require(self.corpus, "h264_mp4.mp4")
                asset = importer.fingerprint_file(self.corpus.path("h264_mp4.mp4"), "h264_mp4.mp4")
                result = extract.extract_metadata(asset, ffprobe=self.ffprobe, **options)
                self.assertEqual([i["code"] for i in result["issues"]], [code])

    def test_latin1_tag_bytes_do_not_break_extraction(self):
        support.require(self.corpus, "mjpeg_avi.avi")
        legacy = self.derived("latin1_tag.avi")
        subprocess.run(
            [support.FFMPEG, "-v", "error", "-y", "-i", str(self.corpus.path("mjpeg_avi.avi")), "-c", "copy",
             "-metadata", "software=caf\xe9", str(legacy)], check=True)
        result = extract.extract_metadata(importer.fingerprint_file(legacy, "latin1_tag.avi"), ffprobe=self.ffprobe)
        self.assertEqual([i for i in result["issues"] if i["severity"] == "error"], [])
        self.assertEqual(result["technical"]["width"], 320)


if __name__ == "__main__":
    unittest.main()

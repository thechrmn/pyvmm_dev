import atexit
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
from tests import support
from tests.support import ROOT, VALID_METADATA, VIDEOS, TempDirCase, importer, read_report, records_by_name, run_main

ISO = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")
ACQUISITION = ("hardware_and_acquisition", "acquisition")


def acquisition(record):
    return record["bestmeta"].get("hardware_and_acquisition", {}).get("acquisition", {})


def iso_seconds(text):
    hours, minutes, seconds = ISO.match(text).groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def error_codes(record):
    return [i["code"] for i in record["issues"] if i["severity"] == "error"]


class PipelineCase(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = support.corpus()

    def write_metadata(self, content=None, name="metadata.json"):
        path = self.tmp / name
        path.write_text(json.dumps(VALID_METADATA if content is None else content))
        return path

    def run_cli(self, *argv, out="out"):
        status, stdout, stderr = run_main(*argv, "-o", self.out(out))
        return status, stdout, stderr

    def summary(self, stdout):
        return json.loads(stdout.strip().splitlines()[-1])


class Oracle(PipelineCase):
    """Whole corpus, judged against how the videos were generated."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.out_dir = Path(tempfile.mkdtemp(prefix="pyvmm-oracle-"))
        atexit.register(shutil.rmtree, cls.out_dir, ignore_errors=True)
        cls.status, cls.stdout, cls.stderr = run_main(cls.corpus.root, "-o", cls.out_dir / "out")
        cls.report = read_report(cls.out_dir / "out")
        cls.records = records_by_name(cls.report)

    def test_every_video_file_in_the_tree_is_processed_once(self):
        expected = sorted(
            p.relative_to(self.corpus.root).as_posix()
            for p in self.corpus.root.rglob("*")
            if p.is_file() and p.suffix.lower() in importer.VIDEO_EXTENSIONS
        )
        self.assertEqual(sorted(self.records), expected)
        self.assertEqual(self.report["summary"]["files"], len(expected))
        self.assertNotIn("video_with_txt_extension.txt", self.records)
        self.assertEqual(self.status, 1)  # no researcher metadata: drafts, by design

    def test_aligned_values_match_the_generation_parameters(self):
        for name, (_args, expected, codec, container) in VIDEOS.items():
            with self.subTest(name):
                support.require(self.corpus, name)
                record = self.records[name]
                found = acquisition(record)
                self.assertEqual(error_codes(record), [])
                self.assertEqual(found["video_resolution_width"], expected["width"])
                self.assertEqual(found["video_resolution_height"], expected["height"])
                self.assertEqual(found["video_codec"], codec)
                self.assertEqual(found["video_container_format"], container)
                self.assertEqual(found.get("bit_depth"), expected.get("bit_depth"))
                self.assertEqual(found.get("color_mode"), expected.get("color_mode"))
                self.assertEqual(found.get("total_frame_count"), expected.get("frame_count"))
                if "frame_rate" in expected:
                    self.assertAlmostEqual(found["frame_rate"] / expected["frame_rate"], 1, places=3)
                    self.assertLessEqual(abs(iso_seconds(found["recording_duration"]) - expected["duration_seconds"]),
                                         2 / expected["frame_rate"])
                else:  # raw streams: timing is unknown and must be asked of the researcher
                    self.assertNotIn("frame_rate", found)
                    self.assertNotIn("recording_duration", found)
                    missing = record["validation"]["missing_required"]
                    self.assertIn("hardware_and_acquisition.acquisition.frame_rate", missing)

    def test_lossless_codecs_never_lack_the_bit_depth_bestmeta_requires(self):
        for name in ("ffv1_gray_mkv.mkv", "ffv1_gray_avi.avi", "ffv1_gray10_mkv.mkv", "raw_rgb_avi.avi"):
            with self.subTest(name):
                support.require(self.corpus, name)
                self.assertNotIn("hardware_and_acquisition.acquisition.bit_depth",
                                 self.records[name]["validation"]["missing_required"])

    def test_unreadable_and_hostile_files_are_errors_without_data(self):
        for name in ("truncated.mp4", "zero_bytes.mp4", "garbage.mp4", "text_named.mp4", "concat_redirect.mp4",
                     "hls_redirect.mp4", "evil_ref.mp4"):
            with self.subTest(name):
                record = self.records[name]
                self.assertIn("ffprobe_failed", error_codes(record))
                self.assertEqual(record["extraction"]["technical"], {})
                self.assertEqual(set(record["bestmeta"]), {"dataset_id"})
                self.assertFalse(record["validation"]["valid"])

    def test_identical_files_share_an_identifier_and_are_reported(self):
        first, second = self.records["h264_mp4.mp4"], self.records["duplicate_of_h264.mp4"]
        self.assertEqual(first["bestmeta"]["dataset_id"], second["bestmeta"]["dataset_id"])
        flagged = [i["path"] for i in self.report["import_issues"] if i["code"] == "duplicate_content"]
        self.assertTrue(any(p.endswith("h264_mp4.mp4") or p.endswith("duplicate_of_h264.mp4") for p in flagged))

    def test_run_information_identifies_the_tools_and_the_schema(self):
        run, schema = self.report["run"], self.report["schema"]
        self.assertEqual((run["software"]["name"], run["software"]["version"]), ("PyVMM", main.__version__))
        self.assertTrue(run["ffprobe"]["version"])
        self.assertEqual(run["ffprobe"]["arguments"][:4], ["-v", "warning", "-protocol_whitelist", "file"])
        self.assertEqual((schema["status"], schema["sha256"]), ("available", "01e7fbaef08ee3f46babd2edd4363e700ccccd5a726c56b72644653b6c8cdd9c"))

    def test_the_summary_tells_what_to_supply(self):
        needed = self.report["summary"]["missing_required"]
        for field in ("dataset_title", "dataset_license", "hardware_and_acquisition.hardware"):
            expected = sum(field in r["validation"]["missing_required"] for r in self.records.values())
            self.assertEqual(needed[field], expected, field)
        self.assertEqual(needed["dataset_title"], self.report["summary"]["files"])

    def test_unicode_and_dash_names_survive(self):
        self.assertIn("ünïcödé 视频 (1).mp4", self.records)
        self.assertIn("-leading_dash.mp4", self.records)


class ExitStatus(PipelineCase):
    def test_zero_when_everything_is_valid(self):
        status, stdout, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--metadata", self.write_metadata())
        self.assertEqual((status, stderr), (0, ""))
        summary = self.summary(stdout)
        self.assertEqual((summary["files"], summary["valid"], summary["success"], summary["missing_required"]), (1, 1, True, {}))
        self.assertTrue(read_report(self.out())["records"][0]["validation"]["valid"])

    def test_one_when_records_are_drafts_and_the_user_is_told_what_to_do(self):
        status, stdout, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"))
        self.assertEqual(status, 1)
        self.assertIn("dataset_title", self.summary(stdout)["missing_required"])
        self.assertIn("1 of 1 records are drafts", stderr)
        self.assertTrue((self.out() / "metadata.json").is_file())

    def test_one_when_a_file_cannot_be_read(self):
        status, stdout, _ = self.run_cli(self.corpus.path("h264_mp4.mp4"), self.corpus.path("garbage.mp4"),
                                         "--metadata", self.write_metadata())
        self.assertEqual(status, 1)
        summary = self.summary(stdout)
        self.assertEqual((summary["files"], summary["valid"], summary["success"]), (2, 1, False))

    def test_one_when_no_input_is_found(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        status, stdout, _ = self.run_cli(empty)
        self.assertEqual(status, 1)
        self.assertEqual(self.summary(stdout)["files"], 0)
        self.assertEqual([i["code"] for i in read_report(self.out())["import_issues"]], ["no_inputs"])

    def test_two_when_it_cannot_run_and_nothing_is_left_behind(self):
        meta = self.tmp / "bad.json"
        meta.write_text("{")
        video = self.corpus.path("h264_mp4.mp4")
        for label, argv in {
            "missing ffprobe": [video, "--ffprobe", "no-such-ffprobe"],
            "bad metadata": [video, "--metadata", meta],
            "missing metadata": [video, "--metadata", self.tmp / "none.json"],
        }.items():
            with self.subTest(label):
                status, stdout, stderr = self.run_cli(*argv, out=label.replace(" ", "_"))
                self.assertEqual((status, stdout), (2, ""))
                self.assertTrue(stderr.startswith("Error:"))
                self.assertFalse(self.out(label.replace(" ", "_")).exists())

    def test_two_and_untouched_when_the_output_exists(self):
        self.out().mkdir()
        (self.out() / "keep.txt").write_text("mine")
        status, stdout, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"))
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("exists", stderr.lower())
        self.assertEqual([p.name for p in self.out().iterdir()], ["keep.txt"])

    def test_two_for_usage_errors(self):
        video = str(self.corpus.path("h264_mp4.mp4"))
        for argv in (["-o", str(self.out())], [video], [video, "-o", str(self.out()), "--timeout", "0"],
                     [video, "-o", str(self.out()), "--timeout", "nan"],
                     [video, "-o", str(self.out()), "--timeout", "inf"], [video, "-o", str(self.out()), "--timeout=-5"],
                     [video, "-o", str(self.out()), "--max-output-bytes", "0"], [video, "-o", str(self.out()), "--max-output-bytes", "-1"],
                     [video, "-o", str(self.out()), "--bogus"]):
            with self.subTest(argv):
                self.assertEqual(run_main(*argv)[0], 2)

    def test_130_on_interrupt_and_nothing_is_left_behind(self):
        with mock.patch.object(main, "import_files", side_effect=KeyboardInterrupt):
            status, _, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"))
        self.assertEqual(status, 130)
        self.assertIn("Interrupted", stderr)
        self.assertFalse(self.out().exists())

    def test_an_unusable_schema_is_announced_and_everything_is_still_exported(self):
        status, stdout, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--schema", self.tmp / "none.json")
        self.assertEqual(status, 1)
        self.assertIn("schema_unavailable", stderr)
        record = read_report(self.out())["records"][0]
        self.assertEqual(acquisition(record)["video_resolution_width"], 320)
        self.assertEqual(record["validation"]["schema_status"], "unavailable")

    def test_without_the_jsonschema_package_drafts_are_still_produced(self):
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            status, _, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--metadata", self.write_metadata())
        self.assertEqual(status, 1)
        self.assertIn("jsonschema_not_installed", stderr)
        report = read_report(self.out())
        self.assertEqual(report["schema"]["status"], "unavailable")
        self.assertEqual(acquisition(report["records"][0])["video_codec"], "h264")

    def test_documented_defaults(self):
        args = main._parser().parse_args(["x", "-o", "y"])
        self.assertEqual(
            (args.timeout, args.max_output_bytes, args.ffprobe, args.extensions, args.schema, args.metadata),
            (60, 16 * 1024 * 1024, "ffprobe", None, None, None),
        )

    def test_version(self):
        status, stdout, _ = run_main("--version")
        self.assertEqual((status, stdout.strip()), (0, f"PyVMM {main.__version__}"))


class BatchValidity(PipelineCase):
    """What the first draft could not do: one researcher file validating a mixed batch."""

    NAMES = ("h264_mp4.mp4", "vp9_webm.webm", "ffv1_gray_mkv.mkv", "ffv1_gray10_mkv.mkv", "mjpeg_avi.avi",
             "prores_mov.mov", "mpeg2_ts.ts", "theora_ogv.ogv", "raw_rgb_avi.avi", "h265_mp4.mp4", "av1_mp4.mp4")

    def test_formats_outside_the_schema_enums_validate_through_other(self):
        for name in self.NAMES:
            support.require(self.corpus, name)
        status, stdout, _ = self.run_cli(*[self.corpus.path(n) for n in self.NAMES], "--metadata", self.write_metadata())
        self.assertEqual(status, 0, self.summary(stdout))
        summary = self.summary(stdout)
        self.assertEqual((summary["files"], summary["valid"], summary["errors"]), (len(self.NAMES), len(self.NAMES), 0))

    def test_files_of_different_lengths_share_one_metadata_file(self):
        names = ("highspeed_1000fps.avi", "h264_mp4.mp4", "single_frame.mp4")
        status, stdout, _ = self.run_cli(*[self.corpus.path(n) for n in names], "--metadata", self.write_metadata())
        self.assertEqual(status, 0)
        durations = {r["extraction"]["asset"]["relative_path"]: acquisition(r)["recording_duration"]
                     for r in read_report(self.out())["records"]}
        self.assertEqual(len(set(durations.values())), 3)

    def test_a_wrong_researcher_claim_about_a_measured_fact_is_caught(self):
        wrong = json.loads(json.dumps(VALID_METADATA))
        wrong["hardware_and_acquisition"]["acquisition"]["video_resolution_width"] = 999
        status, stdout, _ = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--metadata", self.write_metadata(wrong))
        self.assertEqual(status, 1)
        record = read_report(self.out())["records"][0]
        self.assertEqual(acquisition(record)["video_resolution_width"], 320)
        self.assertEqual(record["conflicts"][0]["annotation"], 999)

    def test_capture_rate_of_a_high_speed_recording_can_be_stated(self):
        meta = json.loads(json.dumps(VALID_METADATA))
        meta["hardware_and_acquisition"]["acquisition"].update({"frame_rate": 5000, "recording_duration": "PT0.1S"})
        status, _, _ = self.run_cli(self.corpus.path("highspeed_1000fps.avi"), "--metadata", self.write_metadata(meta))
        self.assertEqual(status, 0)
        record = read_report(self.out())["records"][0]
        self.assertEqual(acquisition(record)["frame_rate"], 5000)
        self.assertEqual(record["extraction"]["technical"]["frame_rate"], 1000.0)  # the file's own rate stays visible

    def test_untimed_raw_streams_validate_once_the_researcher_supplies_the_rate(self):
        meta = json.loads(json.dumps(VALID_METADATA))
        meta["hardware_and_acquisition"]["acquisition"].update({"frame_rate": 30, "recording_duration": "PT2S"})
        status, stdout, _ = self.run_cli(self.corpus.path("raw_stream.h264"), "--metadata", self.write_metadata(meta))
        self.assertEqual(status, 0, self.summary(stdout))


class Safety(PipelineCase):
    def test_files_that_name_other_files_are_not_followed(self):
        status, _, _ = self.run_cli(self.corpus.path("concat_redirect.mp4"), self.corpus.path("hls_redirect.mp4"),
                                    self.corpus.path("evil.ffconcat"), self.corpus.path("evil.m3u8"))
        self.assertEqual(status, 1)
        for record in read_report(self.out())["records"]:
            self.assertEqual(record["extraction"]["technical"], {})
            self.assertEqual(record["extraction"]["raw"], {})

    def test_shell_metacharacters_in_names_do_nothing(self):
        folder = self.tmp / "evil names"
        folder.mkdir()
        names = ["a;touch PWNED;.mp4", "$(touch PWNED2).mp4", "`touch PWNED3`.mp4", "x' || touch PWNED4 || '.mp4",
                 "-i.mp4", "--help.mp4", "file:x.mp4", "concat:a|b.mp4", "pipe:0.mp4", "http:x.mp4"]
        if os.name == "nt":  # Windows forbids some of these characters in file names
            names = [name for name in names if not set(name) & set('<>:"/\\|?*')]
        for name in names:
            shutil.copy(self.corpus.path("h264_mp4.mp4"), folder / name)
        marker_dir = self.tmp / "cwd"
        marker_dir.mkdir()
        cwd = Path.cwd()
        os.chdir(marker_dir)
        self.addCleanup(os.chdir, cwd)
        status, stdout, _ = self.run_cli(folder, "--metadata", self.write_metadata())
        self.assertEqual(self.summary(stdout)["files"], len(names))
        self.assertEqual(sorted(p.name for p in marker_dir.iterdir()), [])
        self.assertEqual(sorted(p.name for p in folder.iterdir() if p.name.startswith("PWNED")), [])
        self.assertEqual(sorted(records_by_name(read_report(self.out()))), sorted(names))
        for record in read_report(self.out())["records"]:
            self.assertEqual(error_codes(record), [], record["extraction"]["asset"]["relative_path"])

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell")
    def test_a_hanging_ffprobe_cannot_stall_the_run(self):
        probe = support.fake_ffprobe(self.tmp, 'case "$1" in -version) echo "ffprobe version 9.9"; exit 0;; esac\nexec sleep 60')
        status, stdout, _ = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--ffprobe", probe, "--timeout", "1")
        self.assertEqual(status, 1)
        self.assertIn("ffprobe_timeout", [i["code"] for i in read_report(self.out())["records"][0]["issues"]])

    def test_extreme_researcher_metadata_is_refused_not_crashed(self):
        for label, content in {"nan": '{"a": NaN}', "duplicate": '{"a": 1, "a": 2}', "deep": "[" * 100_000,
                               "huge number": "1" + "0" * 10_000, "overflow": '{"a": 1e999}'}.items():
            with self.subTest(label):
                path = self.tmp / f"{label}.json"
                path.write_text(content)
                status, _, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"), "--metadata", path, out=label.replace(" ", "_"))
                self.assertEqual(status, 2)
                self.assertTrue(stderr.startswith("Error:"))


class Behaviour(PipelineCase):
    def test_output_is_byte_for_byte_reproducible(self):
        args = [self.corpus.path("h264_mp4.mp4"), self.corpus.path("vp9_webm.webm"), self.corpus.path("garbage.mp4"),
                "--metadata", self.write_metadata()]
        self.run_cli(*args, out="one")
        self.run_cli(*args, out="two")
        for name in ("metadata.json", "metadata.csv"):
            self.assertEqual((self.out("one") / name).read_bytes(), (self.out("two") / name).read_bytes())

    def test_data_reached_through_a_linked_path_is_processed(self):
        link = self.tmp / "linked"
        try:
            os.symlink(self.corpus.root, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        status, stdout, _ = self.run_cli(link)
        self.assertGreater(self.summary(stdout)["files"], 20)

    def test_an_output_folder_inside_the_input_tree_is_never_read_back(self):
        data = self.tmp / "data"
        data.mkdir()
        shutil.copy(self.corpus.path("h264_mp4.mp4"), data / "a.mp4")
        shutil.copy(self.corpus.path("mjpeg_avi.avi"), data / "b.avi")
        self.run_cli(data, out="data/results")
        second = self.summary(self.run_cli(data, out="data/results2")[1])
        self.assertEqual(second["files"], 2)

    def test_extension_filter(self):
        data = self.tmp / "data"
        data.mkdir()
        shutil.copy(self.corpus.path("h264_mp4.mp4"), data / "a.mp4")
        shutil.copy(self.corpus.path("mjpeg_avi.avi"), data / "b.avi")
        (data / "c.txt").write_text("not a video")
        count = lambda *extra, out: self.summary(self.run_cli(data, *extra, out=out)[1])["files"]  # noqa: E731
        self.assertEqual(count(out="all"), 2)
        self.assertEqual(count("--extensions", ".avi", out="avi"), 1)
        self.assertEqual(count("--extensions", "mp4,AVI", out="both"), 2)
        self.assertEqual(count("--extensions", "*", out="star"), 3)

    def test_verbose_lists_files_on_stderr_only(self):
        status, stdout, stderr = self.run_cli(self.corpus.path("h264_mp4.mp4"), self.corpus.path("mjpeg_avi.avi"), "-v")
        self.assertIn("[1] h264_mp4.mp4", stderr)
        self.assertIn("[2] mjpeg_avi.avi", stderr)
        self.assertNotIn("[1]", stdout)

    def test_runs_as_a_script_from_any_directory(self):
        result = subprocess.run(
            [sys.executable, "-W", "error", str(ROOT / "main.py"), str(self.corpus.path("h264_mp4.mp4")), "-o", str(self.out()),
             "--metadata", str(self.write_metadata())],
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(json.loads(result.stdout)["success"])
        result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--help"], cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Exit status", result.stdout)


class Fuzzing(PipelineCase):
    """Corrupted media must never crash, hang or poison the output."""

    MUTATIONS = int(os.environ.get("PYVMM_FUZZ_COUNT", "60"))
    SEED = int(os.environ.get("PYVMM_FUZZ_SEED", "20261104"))

    def mutate(self, rng, data):
        data = bytearray(data)
        mode = rng.choice(("truncate", "flip", "zero", "random", "insert", "head", "double"))
        if mode == "truncate":
            return data[: rng.randrange(len(data))]
        if mode == "flip":
            for _ in range(rng.randint(1, 50)):
                data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
        elif mode in ("zero", "random"):
            start = rng.randrange(len(data))
            end = min(len(data), start + rng.randint(1, 2000))
            data[start:end] = bytes(end - start) if mode == "zero" else rng.randbytes(end - start)
        elif mode == "insert":
            start = rng.randrange(len(data))
            data[start:start] = rng.randbytes(rng.randint(1, 500))
        elif mode == "head":
            return data[: rng.randint(1, 64)]
        else:
            return data + data
        return data

    def test_seeded_corruptions(self):
        rng = random.Random(self.SEED)
        wanted = ("h264_mp4.mp4", "mjpeg_avi.avi", "ffv1_gray_mkv.mkv", "vp9_webm.webm", "h264_mov.mov", "mpeg2_ts.ts",
                  "theora_ogv.ogv", "raw_stream.h264")
        bases = [(n, self.corpus.path(n).read_bytes()) for n in wanted if n not in self.corpus.skipped]
        self.assertGreaterEqual(len(bases), 3)
        folder = self.tmp / "fuzz"
        folder.mkdir()
        for i in range(self.MUTATIONS):
            name, data = rng.choice(bases)
            (folder / f"{i:03d}{Path(name).suffix}").write_bytes(self.mutate(rng, data))
        status, stdout, stderr = self.run_cli(folder, "--timeout", "30")
        self.assertIn(status, (0, 1))
        self.assertNotIn("Traceback", stderr)

        def strict(token):
            raise ValueError(token)

        report = json.loads((self.out() / "metadata.json").read_text(encoding="utf-8"), parse_constant=strict)
        self.assertEqual(report["summary"]["files"], self.MUTATIONS)
        for record in report["records"]:
            technical = record["extraction"]["technical"]
            self.assertEqual(set(record), {"extraction", "bestmeta", "annotations", "provenance", "conflicts", "issues", "validation"})
            self.assertIsInstance(record["validation"]["valid"], bool)
            if error_codes(record):
                self.assertFalse(record["validation"]["valid"])
            if "ffprobe_failed" in error_codes(record):
                self.assertEqual((technical, record["extraction"]["raw"]), ({}, {}))
            for key in ("width", "height"):
                if key in technical:
                    self.assertIsInstance(technical[key], int)
                    self.assertGreater(technical[key], 0)
            for key in ("frame_rate", "duration_seconds"):
                if key in technical:
                    self.assertGreaterEqual(technical[key], 0)
            for issue in record["issues"]:
                self.assertEqual(set(issue) - {"path"}, {"severity", "code", "message", "stage"})
        self.assertTrue(any(r["extraction"]["technical"] for r in report["records"]), "fuzzing should leave some files readable")
        self.assertTrue(any(error_codes(r) for r in report["records"]), "fuzzing should break some files")


class Documentation(unittest.TestCase):
    def readme(self):
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_readme_example_is_the_tested_example(self):
        text = self.readme()
        marker = text.index("<!-- example-metadata -->")
        block = re.search(r"```json\n(.*?)\n```", text[marker:], re.S)
        self.assertIsNotNone(block, "README must show the example right after the marker")
        self.assertEqual(json.loads(block.group(1)), VALID_METADATA)

    def test_every_issue_code_the_code_can_raise_is_documented(self):
        pattern = re.compile(r'(?:_issue\((?:issues,\s*)?|ProbeError\()\s*"([a-z_]+)"')
        codes = set()
        for name in ("import.py", "extract.py", "align.py"):
            codes |= set(pattern.findall((ROOT / name).read_text(encoding="utf-8")))
        self.assertGreater(len(codes), 25)
        text = self.readme()
        self.assertEqual(sorted(code for code in codes if f"`{code}`" not in text), [])

    def test_the_readme_states_the_exit_statuses_the_program_uses(self):
        text = self.readme()
        for fragment in ("| 0 |", "| 1 |", "| 2 |", "| 130 |"):
            self.assertIn(fragment, text)

    def test_the_readme_names_the_pinned_schema_commit(self):
        schema_readme = (ROOT / "schemas" / "README.md").read_text(encoding="utf-8")
        self.assertIn("6bf77e27fe274481c06ee9e4b52e2befdcdb36d7", schema_readme)


if __name__ == "__main__":
    unittest.main()

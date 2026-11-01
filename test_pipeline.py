"""Portable regression contracts; run: python -B -m unittest -v test_pipeline."""

import contextlib
import csv
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import align, export, extract, main
else:
    import align
    import export
    import extract
    import main

import_stage = importlib.import_module(f"{__package__}.import" if __package__ else "import")
ACQUISITION = "hardware_and_acquisition.acquisition"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "scientific-λ.mp4"
        self.source.write_bytes(b"local source bytes")
        self.asset = import_stage.fingerprint_file(self.source, self.source.name)

    @staticmethod
    def raw():
        return {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 32,
                    "height": 24,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                    "duration_ts": 1001,
                    "time_base": "1/3000",
                    "nb_frames": "10",
                    "pix_fmt": "gray",
                }
            ],
            "format": {"format_name": "mp4"},
        }

    def probe(self, raw=None, *, response=None):
        response = response or (0, json.dumps(self.raw() if raw is None else raw).encode(), b"")
        with patch.object(extract, "_run_bounded", return_value=response):
            return extract.extract_metadata(self.asset, ffprobe="unused")

    def test_discovery_hashes_and_duplicate_content(self):
        copy = self.root / "copy.mp4"
        copy.write_bytes(self.source.read_bytes())
        (self.root / "ignore.txt").write_text("irrelevant", encoding="utf-8")
        assets, issues = import_stage.import_files([self.root, self.source])
        self.assertEqual(len(assets), 2)
        self.assertEqual({a["sha256"] for a in assets}, {self.asset["sha256"]})
        self.assertTrue({"duplicate_path", "duplicate_content"} <= {i["code"] for i in issues})
        self.assertEqual(
            self.asset["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest()
        )
        self.assertEqual(assets, import_stage.import_files([self.root, self.source])[0])

    def test_explicit_unknown_extension_and_bad_input_are_independent(self):
        renamed = self.root / "unknown.science"
        self.source.rename(renamed)
        assets, issues = import_stage.import_files([renamed, self.root / "missing"])
        self.assertEqual([a["relative_path"] for a in assets], [renamed.name])
        self.assertTrue(any(i["severity"] == "error" for i in issues))

    def test_aliases_are_skipped(self):
        alias = self.root / "alias.mp4"
        try:
            alias.symlink_to(self.source)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        assets, issues = import_stage.import_files([alias, self.source])
        self.assertEqual(len(assets), 1)
        self.assertIn("symlink_skipped", {i["code"] for i in issues})

    def test_large_hash_and_every_snapshot_field(self):
        payload = bytes(range(256)) * 9000
        self.source.write_bytes(payload)
        asset = import_stage.fingerprint_file(self.source, self.source.name)
        self.assertEqual(asset["sha256"], hashlib.sha256(payload).hexdigest())
        for field in ("size_bytes", "mtime_ns", "ctime_ns", "device_id", "inode"):
            with self.subTest(field=field), patch.object(extract, "_run_bounded") as probe:
                altered = dict(asset, **{field: asset[field] + 1})
                result = extract.extract_metadata(altered, ffprobe="unused")
                self.assertIn("input_changed", {i["code"] for i in result["issues"]})
                probe.assert_not_called()

    def test_mutation_during_probe_is_reported(self):
        def mutate(*args):
            self.source.write_bytes(b"changed and longer than the original")
            return 0, json.dumps(self.raw()).encode(), b""

        with patch.object(extract, "_run_bounded", side_effect=mutate):
            result = extract.extract_metadata(self.asset, ffprobe="unused")
        self.assertIn("input_changed", {i["code"] for i in result["issues"]})

    def test_malformed_probe_responses_fail_per_file(self):
        responses = (
            b"not JSON",
            b"[]",
            b'{"streams":[],"streams":[]}',
            b'{"x":NaN}',
            b'{"x":1e999}',
            b'{"streams":[0]}',
            b'{"streams":[{"index":0},{"index":0}]}',
            b'{"streams":[{"index":true}]}',
            b"\xff",
            b"[" * 2000,
        )
        for response in responses:
            with self.subTest(response=response[:60]):
                result = self.probe(response=(0, response, b""))
                self.assertTrue(any(i["severity"] == "error" for i in result["issues"]))

    def test_probe_failure_preserves_available_evidence(self):
        result = self.probe(response=(1, json.dumps(self.raw()).encode(), b"damaged trailer"))
        self.assertEqual(result["technical"]["width"], 32)
        self.assertIn("ffprobe_failed", {i["code"] for i in result["issues"]})

    def test_stream_selection_and_ambiguous_container_duration(self):
        raw = self.raw()
        stream = raw["streams"][0]
        del stream["duration_ts"], stream["time_base"]
        raw["format"]["duration"] = "10"
        raw["streams"] = [
            dict(stream, index=0, disposition={"attached_pic": 1, "default": 1}),
            dict(stream, index=4),
            dict(stream, index=7),
        ]
        for default, selected in ((1, 7), (0, 4)):
            with self.subTest(default=default):
                raw["streams"][2]["disposition"] = {"default": default}
                result = self.probe(raw)
                self.assertEqual(result["technical"]["stream_index"], selected)
                self.assertNotIn("duration_seconds", result["technical"])
                self.assertEqual(result["technical"]["container_duration_seconds"], 10)
                self.assertIn("video_duration_ambiguous", {i["code"] for i in result["issues"]})

    def test_exact_playback_timing_never_becomes_capture_timing(self):
        extraction = self.probe()
        self.assertEqual(extraction["technical"]["frame_rate_rational"], "30000/1001")
        self.assertEqual(extraction["technical"]["duration_rational"], "1001/3000")
        record = align.align_metadata(extraction)
        acquisition = record["bestmeta"]["hardware_and_acquisition"]["acquisition"]
        self.assertNotIn("frame_rate", acquisition)
        self.assertNotIn("recording_duration", acquisition)
        supplied = {"hardware_and_acquisition": {"acquisition": {"frame_rate": 1}}}
        enriched = align.align_metadata(extraction, supplied)
        self.assertFalse(enriched["conflicts"])
        self.assertEqual(
            enriched["bestmeta"]["hardware_and_acquisition"]["acquisition"]["frame_rate"], 1
        )
        raw = self.raw()
        del raw["streams"][0]["nb_frames"]
        self.assertNotIn("frame_count", self.probe(raw)["technical"])

    def test_conflicts_preserve_both_candidates_and_bool_is_not_integer(self):
        for candidate in (33, True):
            with self.subTest(candidate=candidate):
                annotations = {
                    "hardware_and_acquisition": {
                        "acquisition": {"video_resolution_width": candidate}
                    }
                }
                extraction = self.probe()
                if candidate is True:
                    extraction["technical"]["width"] = 1
                record = align.align_metadata(extraction, annotations)
                path = ACQUISITION + ".video_resolution_width"
                self.assertEqual(record["conflicts"][0]["annotation"], candidate)
                self.assertEqual(
                    [e["selected"] for e in record["provenance"][path]], [True, False]
                )
                selected = record["bestmeta"]["hardware_and_acquisition"]["acquisition"][
                    "video_resolution_width"
                ]
                self.assertEqual(selected, extraction["technical"]["width"])
                self.assertIs(type(selected), int)
                self.assertFalse(record["validation"]["valid"])

    def test_scientific_empty_physical_and_nanosecond_temporal_checks(self):
        acquisition = {
            "frame_rate": -1,
            "recording_start_datetime": "2026-01-01T00:00:00.000000002Z",
            "recording_end_datetime": "2026-01-01T00:00:00.000000001Z",
        }
        record = align.align_metadata(
            self.probe(),
            {
                "dataset_title": None,
                "hardware_and_acquisition": {"acquisition": acquisition},
            },
        )
        diagnostics = {(e["path"], e["code"]) for e in record["validation"]["errors"]}
        for expected in (
            ("dataset_title", "scientific_required_empty"),
            (ACQUISITION + ".frame_rate", "nonpositive_number"),
            (ACQUISITION + ".recording_end_datetime", "temporal_order"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, diagnostics)

    def test_annotation_envelope_is_detached_and_bad_json_is_rejected(self):
        cyclic = []
        cyclic.append(cyclic)
        nested = {}
        for _ in range(70):
            nested = {"next": nested}
        for value in (
            [],
            {"bestmeta": []},
            {"bestmeta": {}, "extra": 1},
            {"annotations": {"bad": float("nan")}},
            {1: "bad"},
            {"annotations": {"cycle": cyclic}},
            nested,
            {"bad": "\ud800"},
        ):
            with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                align.validate_annotations(value)
        original = {"annotations": {"values": [1]}}
        detached = align.validate_annotations(original)
        detached["annotations"]["values"].append(2)
        self.assertEqual(original["annotations"]["values"], [1])

    def test_uri_fields_validate_the_complete_value(self):
        fields = (
            "dataset_doi",
            "publication_doi",
            "analysis_code_doi",
            "raw_tracking_data_doi",
            "analysis_code_repository_url",
            "raw_data_repository_url",
            "dataset_creator_orcid",
        )
        for field in fields:
            for suffix in ("", "\n"):
                with self.subTest(field=field, suffix=repr(suffix)):
                    value = "https://example.org/reference" + suffix
                    candidate = [value] if field == "dataset_creator_orcid" else value
                    record = align.align_metadata(self.probe(), {field: candidate})
                    failures = [
                        e
                        for e in record["validation"]["errors"]
                        if e["code"] == "format" and e["path"].split(".")[0] == field
                    ]
                    self.assertEqual(bool(failures), bool(suffix))

    def test_anchored_scientific_patterns_require_complete_ascii_values(self):
        fields = {
            "dataset_version": "1.0.0",
            "dataset_contact_email": "a1@example.org",
            ACQUISITION + ".recording_duration": "PT1S",
            "experimental_conditions.subject.0.species_ncbi_taxon_id": "7955",
            "experimental_conditions.manipulation.exposure_compound_chebi_id": "CHEBI:15377",
        }
        digits = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
        for path, clean in fields.items():
            for value in (clean, clean + "\n", clean.translate(digits)):
                with self.subTest(path=path, value=value):
                    metadata = {
                        "hardware_and_acquisition": {"acquisition": {}},
                        "experimental_conditions": {"subject": [{}], "manipulation": {}},
                    }
                    parts = path.split(".")
                    target = metadata
                    for part in parts[:-1]:
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    target[parts[-1]] = value
                    record = align.align_metadata(self.probe(), metadata)
                    failures = [
                        e
                        for e in record["validation"]["errors"]
                        if e["path"] == path and e["code"] in ("pattern", "scientific_pattern")
                    ]
                    self.assertEqual(bool(failures), value != clean)

    def test_json_file_limits_and_duplicate_keys(self):
        path = self.root / "context.json"
        for content in ('{"a":1,"a":2}', '{"a":NaN}', "[]", '{"a":1e999}'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ValueError):
                align.read_json(path)
        path.write_text('{"a":"' + "x" * 65 + '"}', encoding="utf-8")
        with patch.object(align, "MAX_METADATA_BYTES", 64), self.assertRaises(ValueError):
            align.read_json(path)

    def test_json_float_extremes_do_not_silently_change_nonzero_values(self):
        path = self.root / "numeric-context.json"
        for literal in ("1e-400", "-1e-400", "1e400", "-1e400", "-0.001E-400"):
            with self.subTest(literal=literal), self.assertRaises(ValueError):
                path.write_text('{"annotations":{"p_value":' + literal + "}}", encoding="utf-8")
                align.read_json(path)
        for literal, expected in (
            ("0e-400", 0.0),
            ("-0e400", -0.0),
            ("0.0000E-400", 0.0),
            ("5e-324", 5e-324),
            ("1e-323", 1e-323),
            ("-1e-323", -1e-323),
            ("1.7976931348623157e308", 1.7976931348623157e308),
        ):
            with self.subTest(literal=literal):
                path.write_text('{"annotations":{"p_value":' + literal + "}}", encoding="utf-8")
                self.assertEqual(align.read_json(path)["annotations"]["p_value"], expected)

    def test_supported_timestamp_profile_is_explicit(self):
        stamps = (
            ("0000-01-01T00:00:00Z", False),
            ("2016-12-31T23:59:60Z", False),
            ("0001-01-01T00:00:00Z", True),
            ("9999-12-31T23:59:59+00:00", True),
            ("2026-01-01t00:00:00z", True),
            ("2026-01-01T00:00:00-00:00", True),
        )
        for prefix in ("recording", "experiment"):
            for endpoint in ("start", "end"):
                key = f"{prefix}_{endpoint}_datetime"
                path = (
                    ACQUISITION + "." + key
                    if prefix == "recording"
                    else "experimental_conditions.experiment." + key
                )
                for stamp, supported in stamps:
                    with self.subTest(path=path, stamp=stamp):
                        metadata = (
                            {"hardware_and_acquisition": {"acquisition": {key: stamp}}}
                            if prefix == "recording"
                            else {"experimental_conditions": {"experiment": {key: stamp}}}
                        )
                        record = align.align_metadata(self.probe(), metadata)
                        failures = [
                            e
                            for e in record["validation"]["errors"]
                            if e["path"] == path and e["code"] == "scientific_datetime_format"
                        ]
                        self.assertEqual(bool(failures), not supported)
                        if failures:
                            self.assertIn("0001 to 9999", failures[0]["message"])
                            self.assertIn("leap seconds are unsupported", failures[0]["message"])

    def test_runtime_lock_package_metadata_and_provenance_agree(self):
        folder = Path(__file__).parent
        lock = (folder / "requirements.txt").read_text(encoding="utf-8")
        pins = dict(re.findall(r"^([A-Za-z0-9_-]+)==([^\s;\\]+)", lock, re.MULTILINE))
        if __package__:
            dependencies = main.package_metadata.requires("pyvmm")
        else:
            project = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
            dependencies = project["project"]["dependencies"]
        self.assertEqual(dict(specification.split("==") for specification in dependencies), pins)
        self.assertEqual(set(main.RUNTIME_DISTRIBUTIONS), set(pins))
        self.assertEqual({name: main.package_metadata.version(name) for name in pins}, pins)

    def test_version_is_available_without_runtime_dependencies(self):
        with (
            patch.object(main, "_dependency_problem", "unavailable dependencies"),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as caught,
        ):
            main.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(main.__version__, output.getvalue())

    def test_reference_manifest_is_complete_and_has_safe_relative_paths(self):
        manifest = align.read_json(Path(__file__).with_name("reference_videos.json"))
        files = manifest["files"]
        self.assertEqual(manifest["summary"]["files"], len(files))
        self.assertEqual(len({entry["relative_path"] for entry in files}), len(files))
        self.assertEqual(
            sum(entry["size_bytes"] for entry in files), manifest["summary"]["total_size_bytes"]
        )
        self.assertEqual(
            sum(entry["expected"]["full_decode_frame_count"] for entry in files),
            manifest["summary"]["full_decode_frames"],
        )
        for entry in files:
            with self.subTest(path=entry["relative_path"]):
                relative = Path(entry["relative_path"])
                self.assertFalse(relative.is_absolute() or ".." in relative.parts)
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(entry["source_page"] and entry["download_url"])
                self.assertTrue(entry["rights"]["statement"])

    def test_export_is_lossless_and_csv_is_a_safe_view(self):
        record = align.align_metadata(
            self.probe(),
            {
                "bestmeta": {"dataset_description": "科学\nMultiline free text\n"},
                "annotations": {"a.b": "=SUM(1,2)", "~": [1, {"k": "λ"}], "tab": "\tformula"},
            },
        )
        self.assertFalse(
            any(e["path"] == "dataset_description" for e in record["validation"]["errors"])
        )
        destination = self.root / "results"
        summary = export.export_metadata([record], destination)
        payload = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["records"], [record])
        self.assertEqual(payload["summary"], summary)
        self.assertEqual(summary["files"], 1)
        with (destination / "metadata.csv").open(encoding="utf-8-sig", newline="") as stream:
            (row,) = csv.DictReader(stream)
        self.assertEqual(row["annotations.a~1b"], "'=SUM(1,2)")
        self.assertEqual(json.loads(row["annotations.~0"]), [1, {"k": "λ"}])
        self.assertEqual(row["annotations.tab"], "'\tformula")

    def test_failed_export_rolls_back_and_existing_output_is_preserved(self):
        destination = self.root / "results"
        record = align.align_metadata(self.probe())

        def failing_records():
            yield record
            raise OSError("upstream failure")

        with self.assertRaisesRegex(OSError, "upstream failure"):
            export.export_metadata(failing_records(), destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".pyvmm-*")))
        destination.mkdir()
        sentinel = destination / "keep"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            export.export_metadata([record], destination)
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_main_preserves_failure_and_interrupt_cleanup_diagnostics(self):
        original_exists = Path.exists

        def exists(path):
            if path.name.startswith(".pyvmm-"):
                raise PermissionError("cleanup denied")
            return original_exists(path)

        for error, expected, message in (
            (RuntimeError("original upstream failure"), 2, "original upstream failure"),
            (KeyboardInterrupt(), 130, "Interrupted."),
        ):
            with self.subTest(exit=expected):
                destination = self.root / f"results-{expected}"
                with (
                    patch.object(main, "inspect_ffprobe", return_value={"executable": "unused"}),
                    patch.object(main, "extract_metadata", return_value={}),
                    patch.object(main, "align_metadata", side_effect=error),
                    patch.object(Path, "exists", exists),
                    contextlib.redirect_stderr(io.StringIO()) as diagnostic,
                ):
                    result = main.main([str(self.source), "-o", str(destination)])
                self.assertEqual(result, expected)
                self.assertIn(message, diagnostic.getvalue())
                self.assertIn("cleanup denied", diagnostic.getvalue())
                self.assertFalse(destination.exists())

    def test_native_process_budget_and_stdin_eof(self):
        command = [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"]
        code, output, _ = extract._run_bounded(command, 10, 1024)
        self.assertEqual((code, output.strip()), (0, b"0"))
        for script, timeout, limit, code in (
            ("print('x'*10000)", 10, 1024, "ffprobe_output_limit"),
            ("import time; time.sleep(60)", 0.05, 1024, "ffprobe_timeout"),
        ):
            with self.subTest(code=code), self.assertRaises(extract.ProbeFailure) as caught:
                extract._run_bounded([sys.executable, "-c", script], timeout, limit)
            self.assertEqual(caught.exception.code, code)

    def test_main_runs_all_stages_for_draft_and_valid_context(self):
        # Fictional context belongs only to this controlled, mocked-probe fixture.
        context = {
            "dataset_id": "fictional-test-fixture",
            "dataset_title": "Fictional microscope context for regression tests",
            "dataset_license": "CC-BY-4.0",
            "hardware_and_acquisition": {
                "hardware": {
                    "device_type": "microscope",
                    "microscope_manufacturer": "Fictional",
                    "microscope_model": "Fixture",
                    "microscope_type": "other",
                    "field_of_view_width": 2,
                    "field_of_view_height": 1.5,
                    "field_of_view_unit": "mm",
                    "objective_magnification": 10,
                    "contrast_polarity": "dark_on_bright",
                },
                "acquisition": {
                    "recording_software_name": "Mock fixture",
                    "recording_software_version": "1",
                    "frame_rate": 10,
                    "recording_duration": "PT1S",
                },
            },
        }
        metadata = self.root / "context.json"
        metadata.write_text(json.dumps(context), encoding="utf-8")
        probe = {"name": "ffprobe", "executable": "unused", "version": "test"}
        for supplied, expected in ((None, 1), (metadata, 0)):
            with self.subTest(metadata=supplied is not None):
                destination = self.root / f"results-{expected}"
                argv = [str(self.source), "-o", str(destination)]
                if supplied is not None:
                    argv += ["--metadata", str(supplied)]
                with (
                    patch.object(main, "inspect_ffprobe", return_value=probe),
                    patch.object(
                        extract,
                        "_run_bounded",
                        return_value=(0, json.dumps(self.raw()).encode(), b""),
                    ),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    result = main.main(argv)
                self.assertEqual(result, expected)
                summary = json.loads(output.getvalue())
                self.assertEqual(summary["success"], expected == 0)
                self.assertEqual(summary["draft"], expected)
                self.assertTrue((destination / "metadata.json").is_file())
                self.assertTrue((destination / "metadata.csv").is_file())

    def test_missing_distribution_metadata_is_a_configuration_error(self):
        destination = self.root / "results"
        with (
            patch.object(main, "inspect_ffprobe", return_value={"executable": "unused"}),
            patch.object(
                main.package_metadata,
                "version",
                side_effect=main.package_metadata.PackageNotFoundError("jsonschema"),
            ),
            patch.object(main, "import_files") as importer,
            contextlib.redirect_stderr(io.StringIO()) as diagnostic,
        ):
            result = main.main([str(self.source), "-o", str(destination)])
        self.assertEqual(result, 2)
        self.assertIn("jsonschema", diagnostic.getvalue())
        importer.assert_not_called()
        self.assertFalse(destination.exists())

    @unittest.skipUnless(
        os.environ.get("PYVMM_FFPROBE"), "Set PYVMM_FFPROBE for native qualification."
    )
    def test_real_ffprobe_reports_invalid_video_without_changing_source(self):
        tool = extract.inspect_ffprobe(os.environ["PYVMM_FFPROBE"])
        with Path(tool["executable"]).open("rb") as stream:
            self.assertEqual(tool["sha256"], hashlib.file_digest(stream, "sha256").hexdigest())
        result = extract.extract_metadata(self.asset, ffprobe=tool["executable"])
        self.assertTrue(any(i["severity"] == "error" for i in result["issues"]))
        self.assertEqual(import_stage.fingerprint_file(self.source, self.source.name), self.asset)

    @unittest.skipUnless(
        os.environ.get("PYVMM_FFPROBE") and os.environ.get("PYVMM_FFMPEG"),
        "Set PYVMM_FFPROBE and PYVMM_FFMPEG for native end-to-end qualification.",
    )
    def test_native_video_runs_all_stages_and_records_execution_provenance(self):
        video = self.root / "controlled.avi"
        subprocess.run(
            [
                os.environ["PYVMM_FFMPEG"],
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=40x30:r=5",
                "-frames:v",
                "10",
                "-c:v",
                "mpeg4",
                str(video),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = self.root / "results"
        arguments = [str(video), "-o", str(output), "--ffprobe", os.environ["PYVMM_FFPROBE"]]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(arguments), 1)
        document = align.read_json(output / "metadata.json")
        (record,) = document["records"]
        technical = record["extraction"]["technical"]
        self.assertFalse(any(i["severity"] == "error" for i in record["extraction"]["issues"]))
        for key, expected in {
            "width": 40,
            "height": 30,
            "codec": "mpeg4",
            "frame_count": 10,
            "frame_rate_rational": "5/1",
            "duration_rational": "2/1",
        }.items():
            self.assertEqual(technical[key], expected)
        run = document["run"]
        self.assertEqual(run["software"]["version"], main.__version__)
        self.assertEqual(run["arguments"], arguments)
        self.assertEqual(
            datetime.fromisoformat(run["started_at_utc"]).utcoffset().total_seconds(), 0
        )
        self.assertTrue(run["environment"]["system"] and run["environment"]["machine"])
        self.assertEqual(set(run["libraries"]), set(main.RUNTIME_DISTRIBUTIONS))
        self.assertEqual(
            run["runtime_lock_sha256"],
            hashlib.sha256(Path(__file__).with_name("requirements.txt").read_bytes()).hexdigest(),
        )

    @unittest.skipUnless(
        os.environ.get("PYVMM_FFPROBE") and os.environ.get("PYVMM_REFERENCE_DATA"),
        "Set PYVMM_FFPROBE and PYVMM_REFERENCE_DATA for public-reference qualification.",
    )
    def test_public_scientific_reference_videos(self):
        manifest = align.read_json(Path(__file__).with_name("reference_videos.json"))
        root = Path(os.environ["PYVMM_REFERENCE_DATA"])
        tool = extract.inspect_ffprobe(os.environ["PYVMM_FFPROBE"])
        for entry in manifest["files"]:
            with self.subTest(path=entry["relative_path"]):
                asset = import_stage.fingerprint_file(
                    root / entry["relative_path"], entry["relative_path"]
                )
                self.assertEqual(asset["sha256"], entry["sha256"])
                self.assertEqual(asset["size_bytes"], entry["size_bytes"])
                extraction = extract.extract_metadata(asset, ffprobe=tool["executable"])
                self.assertFalse(any(i["severity"] == "error" for i in extraction["issues"]))
                for key, expected in entry["expected"].items():
                    if key != "full_decode_frame_count":
                        self.assertEqual(extraction["technical"].get(key), expected, key)
                context = entry.get("scientific_context", {})
                if "acquisition_frame_rate_rational" in context:
                    rate = float(Fraction(context["acquisition_frame_rate_rational"]))
                    record = align.align_metadata(
                        extraction,
                        {"hardware_and_acquisition": {"acquisition": {"frame_rate": rate}}},
                    )
                    self.assertFalse(record["conflicts"])
                    acquisition = record["bestmeta"]["hardware_and_acquisition"]["acquisition"]
                    self.assertEqual(acquisition["frame_rate"], rate)
                    self.assertNotIn("recording_duration", acquisition)


if __name__ == "__main__":
    unittest.main()

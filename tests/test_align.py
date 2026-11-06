import base64
import copy
import hashlib
import http.server
import json
import random
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import align
from tests.support import VALID_METADATA, TempDirCase

PINNED_BESTMETA_SHA256 = "01e7fbaef08ee3f46babd2edd4363e700ccccd5a726c56b72644653b6c8cdd9c"
ACQ = ("hardware_and_acquisition", "acquisition")
CODEC = "hardware_and_acquisition.acquisition.video_codec"
SCHEMA = align.load_schema()


class SchemaCase(TempDirCase):
    def custom_schema(self, schema):
        path = self.tmp / "custom-schema.json"
        path.write_text(json.dumps(schema))
        return align.load_schema(path)


def technical(**changes):
    base = {
        "stream_index": 0, "width": 320, "height": 240, "frame_count": 60, "bit_depth": 8,
        "codec": "h264", "pixel_format": "yuv420p", "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "frame_rate": 30.0, "duration_seconds": 2.0, "color_mode": "rgb",
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


def extraction(brand="isom", issues=(), sha="ab" * 32, **changes):
    return {
        "asset": {"path": "/v.mp4", "relative_path": "v.mp4", "size_bytes": 1, "mtime_ns": 1, "sha256": sha},
        "raw": {"format": {"tags": {"major_brand": brand}}},
        "technical": technical(**changes),
        "issues": list(issues),
    }


def align_one(metadata=None, schema=SCHEMA, ex=None, **options):
    envelope = {"bestmeta": {}, "annotations": {}}
    envelope.update(metadata or {})
    return align.align_metadata(ex or extraction(), envelope, schema, **options)


def acq(record, key):
    return record["bestmeta"]["hardware_and_acquisition"]["acquisition"][key]


def issue_codes(record):
    return [i["code"] for i in record["issues"]]


class FormatChecks(unittest.TestCase):
    def check(self, function, valid, invalid):
        for value in valid:
            self.assertIs(function(value), True, f"should accept {value!r}")
        for value in invalid:
            self.assertIs(function(value), False, f"should reject {value!r}")
        for value in (5, None, True, [], {}):
            self.assertIs(function(value), True, "formats apply to strings only")

    def test_date(self):
        self.check(
            align._is_date,
            ["2025-03-01", "2024-02-29"],
            ["2025-02-30", "2023-02-29", "20250301", "2025-3-1", "2025-W09-6", "２０２５-０３-０１", " 2025-03-01",
             "2025-03-01\n", ""],
        )

    def test_date_time(self):
        self.check(
            align._is_datetime,
            ["2025-03-01T10:20:30Z", "2025-03-01t10:20:30z", "2025-03-01T10:20:30.123+02:00",
             "2025-12-31T23:59:60Z", "2025-03-01T10:20:30-23:59"],
            ["2025-03-01T10:20:30", "2025-03-01 10:20:30Z", "2025-13-01T10:20:30Z", "2025-02-30T10:20:30Z",
             "2025-03-01T24:00:00Z", "2025-03-01T10:60:00Z", "2025-03-01T10:20:61Z", "2025-03-01T10:20:30+24:00", "2025-03-01T10:20:30+02:60", "2025-03-01T10:20:30-02:60",
             "2025-03-01T10:20:30+02", "2025-03-01T10:20:30.Z", "0000-01-01T00:00:00Z", "yesterday", "2025-03-01T10:20:30Z\n"],
        )

    def test_uri(self):
        self.check(
            align._is_uri,
            ["https://zenodo.org/records/15237564", "ni:///sha-256;abc_-", "urn:isbn:0451450523", "mailto:a@b.org",
             "http://[::1]:8080/x?y=1#z", "doi:10.1234/abc%20def"],
            ["not a uri", "", "://x", "1http://x", "http://exa mple.org", "http://x/%zz", "http://x/%2",
             "http://x/é", "http://x/<a>", "http://x/\n"],
        )


class DurationFormat(unittest.TestCase):
    PATTERN = re.compile(
        json.loads(Path(align.DEFAULT_SCHEMA).read_text())["$defs"]["Acquisition"]["properties"]["recording_duration"]["pattern"]
    )

    def test_values(self):
        table = {
            0: "PT0S", 2.0: "PT2S", 0.5: "PT0.5S", 1 / 30: "PT0.033333S", 61.5: "PT1M1.5S", 3600: "PT1H",
            3661.25: "PT1H1M1.25S", 86400: "PT24H", 4.9333333333: "PT4.933333S", 4e-7: "PT0S", 6e-7: "PT0.000001S",
            59.9999996: "PT1M", 1e9: "PT277777H46M40S", 7200.000001: "PT2H0.000001S",
        }
        for seconds, want in table.items():
            with self.subTest(seconds):
                text = align._iso_duration(seconds)
                self.assertEqual(text, want)
                self.assertRegex(text, self.PATTERN)


class MetadataFile(TempDirCase):
    def load(self, content, name="meta.json"):
        path = self.tmp / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return align.load_metadata(path)

    def test_no_file_means_no_researcher_input(self):
        self.assertEqual(align.load_metadata(None), {"bestmeta": {}, "annotations": {}})

    def test_bare_object_is_bestmeta(self):
        self.assertEqual(self.load('{"dataset_title": "x"}'), {"bestmeta": {"dataset_title": "x"}, "annotations": {}})

    def test_envelopes(self):
        self.assertEqual(self.load('{"bestmeta": {"a": 1}, "annotations": {"b": 2}}'),
                         {"bestmeta": {"a": 1}, "annotations": {"b": 2}})
        self.assertEqual(self.load('{"annotations": {"b": 2}}'), {"bestmeta": {}, "annotations": {"b": 2}})
        self.assertEqual(self.load('{"bestmeta": {}}'), {"bestmeta": {}, "annotations": {}})

    def test_numbers_keep_their_kind(self):
        loaded = self.load('{"float": 29.97, "int": 3, "exp": 1.5e3, "neg": -0.25}')["bestmeta"]
        self.assertEqual(loaded, {"float": 29.97, "int": 3, "exp": 1500.0, "neg": -0.25})
        self.assertEqual([type(loaded[k]) for k in ("float", "int", "exp")], [float, int, float])

    def test_utf8_bom_and_unicode_are_accepted(self):
        loaded = self.load(b'\xef\xbb\xbf{"dataset_title": "M\xc3\xa4use \xe8\xa7\x86\xe9\xa2\x91"}')
        self.assertEqual(loaded["bestmeta"]["dataset_title"], "Mäuse 视频")

    def test_malformed_files_raise_value_error(self):
        bad = {
            "not json": "{", "empty": "", "list root": "[1]", "scalar root": "3", "duplicate key": '{"a": 1, "a": 2}',
            "nested duplicate": '{"a": {"b": 1, "b": 2}}', "NaN": '{"a": NaN}', "Infinity": '{"a": Infinity}',
            "float overflow": '{"a": 1e999}', "bad utf-8": b'{"a": "\xff"}', "extra envelope key": '{"bestmeta": {}, "x": {}}',
            "envelope member not object": '{"bestmeta": [1]}', "annotations not object": '{"annotations": 5}',
            "deep nesting": "[" * 200_000, "huge integer": '{"a": ' + "9" * 5000 + "}",
        }
        for label, content in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.load(content)

    def test_oversized_file_is_refused_and_one_exactly_at_the_limit_is_read(self):
        self.assertEqual(align.MAX_JSON_BYTES, 16 * 1024 * 1024)
        with mock.patch.object(align, "MAX_JSON_BYTES", 10):
            self.assertEqual(self.load('{"a": 123}')["bestmeta"], {"a": 123})  # exactly 10 bytes
            with self.assertRaisesRegex(ValueError, "exceeds"):
                self.load('{"a": 1234}')  # 11 bytes

    def test_missing_file_and_folders(self):
        with self.assertRaises(OSError):
            align.load_metadata(self.tmp / "nope.json")
        with self.assertRaises((ValueError, OSError)):
            align.load_metadata(self.tmp)


class SchemaLoading(TempDirCase):
    def write(self, content, name="schema.json"):
        path = self.tmp / name
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    def test_bundled_bestmeta_is_the_default_and_matches_its_recorded_provenance(self):
        info = SCHEMA["info"]
        self.assertEqual(info["status"], "available")
        self.assertEqual(info["sha256"], PINNED_BESTMETA_SHA256)
        self.assertEqual(info["sha256"], hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest())
        self.assertEqual(info["dialect"], "https://json-schema.org/draft/2019-09/schema")
        self.assertEqual(SCHEMA["issues"], [])
        self.assertEqual(Path(info["path"]), align.DEFAULT_SCHEMA)
        self.assertIn(PINNED_BESTMETA_SHA256, (Path(align.DEFAULT_SCHEMA).parent / "README.md").read_text())

    def test_explicit_path_to_the_same_file_gives_the_same_result(self):
        again = align.load_schema(align.DEFAULT_SCHEMA)
        self.assertEqual(again["info"], SCHEMA["info"])

    def test_every_failure_degrades_to_an_issue_never_an_exception(self):
        cases = {
            "missing file": (self.tmp / "absent.json", "No such file"),
            "invalid json": (self.write("{", "a.json"), "JSONDecodeError|Expecting"),
            "unsupported dialect": (
                self.write({"$schema": "http://example.com/unknown", "type": "object"}, "b.json"), "Unsupported JSON Schema dialect"),
            "invalid schema": (self.write({"type": 5}, "c.json"), "SchemaError"),
            "duplicate keys": (self.write('{"type": "object", "type": "string"}', "d.json"), "Duplicate object key"),
            "folder": (self.tmp, "Not a regular file"),
        }
        for label, (path, reason) in cases.items():
            with self.subTest(label):
                context = align.load_schema(path)
                self.assertEqual(context["info"]["status"], "unavailable")
                self.assertIsNone(context["validator"])
                self.assertEqual([i["code"] for i in context["issues"]], ["schema_unavailable"])
                self.assertRegex(context["issues"][0]["message"], reason)

    def test_missing_jsonschema_package_is_reported(self):
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            context = align.load_schema()
        self.assertEqual([(i["severity"], i["code"]) for i in context["issues"]], [("warning", "jsonschema_not_installed")])
        self.assertIsNone(context["validator"])

    def test_schema_without_dialect_uses_the_latest_draft(self):
        context = align.load_schema(self.write({"type": "object", "required": ["a"]}))
        self.assertEqual(context["info"]["status"], "available")
        self.assertIn("2020-12", context["info"]["dialect"])

    def test_only_date_date_time_and_uri_formats_are_asserted(self):
        path = self.write({"type": "object", "properties": {
            "mail": {"format": "email"}, "when": {"format": "date-time"}, "site": {"format": "uri"}}})
        validator = align.load_schema(path)["validator"]
        self.assertTrue(validator.is_valid({"mail": "nonsense"}))
        self.assertFalse(validator.is_valid({"when": "yesterday"}))
        self.assertFalse(validator.is_valid({"site": "not a uri"}))

    def test_remote_references_are_never_fetched(self):
        hits = []

        class Recorder(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/s.json"
        context = align.load_schema(self.write({"type": "object", "properties": {"a": {"$ref": url}}}))
        record = align_one({"bestmeta": {"a": 1}}, schema=context)
        self.assertEqual(hits, [])
        self.assertIn("schema_validation_unavailable", issue_codes(record))
        self.assertEqual(record["validation"]["schema_status"], "unavailable")
        self.assertIsNone(record["validation"]["schema_valid"])
        self.assertEqual(acq(record, "video_resolution_width"), 320)  # evidence survives


class Mapping(SchemaCase):
    def test_every_measured_field_reaches_its_bestmeta_destination(self):
        record = align_one()
        for key, want in {
            "video_resolution_width": 320, "video_resolution_height": 240, "frame_rate": 30.0,
            "recording_duration": "PT2S", "total_frame_count": 60, "bit_depth": 8, "color_mode": "rgb",
            "video_codec": "h264", "video_container_format": "mp4",
        }.items():
            self.assertEqual(acq(record, key), want, key)
        self.assertEqual(set(record["bestmeta"]["hardware_and_acquisition"]["acquisition"]), {
            "video_resolution_width", "video_resolution_height", "frame_rate", "recording_duration",
            "total_frame_count", "bit_depth", "color_mode", "video_codec", "video_container_format"})
        self.assertEqual(set(record["bestmeta"]), {"dataset_id", "hardware_and_acquisition"})

    def test_dataset_id_is_the_content_hash_as_an_rfc6920_uri(self):
        digest = hashlib.sha256(b"x").hexdigest()
        record = align_one(ex=extraction(sha=digest))
        expected = "ni:///sha-256;" + base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip("=")
        self.assertEqual(record["bestmeta"]["dataset_id"], expected)
        self.assertRegex(expected, r"^ni:///sha-256;[A-Za-z0-9_-]{43}$")
        for sha in (None, "", "ZZ", "AB" * 32, 5):
            with self.subTest(sha):
                self.assertNotIn("dataset_id", align_one(ex=extraction(sha=sha))["bestmeta"])

    def test_provenance_names_source_and_method_of_every_measurement(self):
        provenance = align_one()["provenance"]
        self.assertEqual(provenance[CODEC], [{
            "source": "extracted", "source_path": "technical.codec", "method": "codec_name_to_BeStMeta",
            "value": "h264", "selected": True}])
        self.assertEqual(provenance[CODEC.replace("video_codec", "recording_duration")][0]["method"],
                         "seconds_to_ISO_8601_duration")
        self.assertEqual(provenance["dataset_id"][0]["method"], "generated_file_view_identifier")

    def test_codec_names_follow_bestmeta(self):
        for name, want in {"h264": "h264", "hevc": "h265", "rawvideo": "raw", "H264": "h264", "mjpeg": "mjpeg",
                           "ffv1": "ffv1", "av1": "av1", "mpeg4": "mpeg4"}.items():
            with self.subTest(name):
                self.assertEqual(acq(align_one(ex=extraction(codec=name)), "video_codec"), want)

    def test_names_the_schema_cannot_express_become_other_and_say_so(self):
        for name in ("vp9", "prores", "mpeg2video", "theora", "dvvideo", "something-new"):
            with self.subTest(name):
                record = align_one(ex=extraction(codec=name))
                self.assertEqual(acq(record, "video_codec"), "other")
                self.assertEqual(record["provenance"][CODEC][0]["method"], "codec_name_to_BeStMeta+other")
                self.assertEqual(record["provenance"][CODEC][0]["source_path"], "technical.codec")

    def test_no_other_without_a_validator(self):
        context = align.load_schema(Path("/nonexistent/schema.json"))
        self.assertEqual(acq(align_one(schema=context, ex=extraction(codec="vp9")), "video_codec"), "vp9")

    def test_container_brand_decides_between_mov_and_mp4(self):
        for brand, want in {"qt  ": "mov", "qt": "mov", "isom": "mp4", "mp42": "mp4", "M4V ": "mp4", "avc1": "mp4",
                            "3gp4": "other", "mjp2": "other", "": "other"}.items():
            with self.subTest(brand):
                self.assertEqual(acq(align_one(ex=extraction(brand=brand)), "video_container_format"), want)

    def test_the_brand_rule_applies_to_the_short_family_name_too(self):
        for brand, want in {"qt": "mov", "isom": "mp4", "3gp4": "other"}.items():
            with self.subTest(brand):
                self.assertEqual(acq(align_one(ex=extraction(brand=brand, container="mov,mp4")), "video_container_format"), want)

    def test_other_containers(self):
        for name, want in {"avi": "avi", "matroska,webm": "other", "mpegts": "other", "ogg": "other", "dv": "other"}.items():
            with self.subTest(name):
                self.assertEqual(acq(align_one(ex=extraction(container=name)), "video_container_format"), want)

    def test_brand_evidence_is_cited_for_iso_base_media(self):
        provenance = align_one()["provenance"]["hardware_and_acquisition.acquisition.video_container_format"][0]
        self.assertEqual((provenance["source_path"], provenance["method"]),
                         ("raw.format.tags.major_brand", "ISOBMFF_major_brand"))

    def test_unmapped_and_absent_values_stay_out(self):
        record = align_one(ex=extraction(frame_count=None, bit_depth=None, color_mode=None))
        for key in ("total_frame_count", "bit_depth", "color_mode"):
            self.assertNotIn(key, record["bestmeta"]["hardware_and_acquisition"]["acquisition"])
        flat = json.dumps(record["bestmeta"])
        for noise in ("pixel_format", "stream_index", "_source"):
            self.assertNotIn(noise, flat)

    def test_a_value_stays_as_measured_when_the_schema_enum_has_no_other(self):
        schema = {"type": "object", "properties": {"c": {"enum": ["a", "b"]}}}
        record = align_one(schema=self.custom_schema(schema), field_map={"codec": ("c",)})
        self.assertEqual(record["bestmeta"]["c"], "h264")
        self.assertEqual([e["code"] for e in record["validation"]["errors"]], ["enum"])

    def test_an_unresolvable_schema_part_does_not_break_the_enum_probe(self):
        schema = {"type": "object", "properties": {"c": {"$ref": "#/$defs/absent"}}}
        record = align_one(schema=self.custom_schema(schema), field_map={"codec": ("c",)})
        self.assertEqual(record["bestmeta"]["c"], "h264")
        self.assertIn("schema_validation_unavailable", issue_codes(record))

    def test_other_is_used_for_any_property_whose_enum_offers_it(self):
        schema = {"type": "object", "properties": {"c": {"enum": ["a", "other"]}}}
        record = align_one(schema=self.custom_schema(schema), field_map={"codec": ("c",)})
        self.assertEqual(record["bestmeta"]["c"], "other")
        self.assertEqual(record["validation"]["errors"], [])

    def test_custom_field_map(self):
        record = align_one(field_map={"width": ("image", "width_px")})
        self.assertEqual(record["bestmeta"]["image"], {"width_px": 320})
        self.assertIn("image.width_px", record["provenance"])

    def test_bad_field_maps_are_rejected(self):
        for bad in ({"w": ("a",), "h": ("a", "b")}, {"w": ()}, {"w": ["a"]}, {"w": ("a", 1)}, {"w": ("dataset_id",)},
                    {"w": ("dataset_id", "x")}):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    align_one(field_map=bad)

    def test_field_map_default_is_not_modified(self):
        before = copy.deepcopy(align.FIELD_MAP)
        align_one(field_map={"width": ("x",)})
        self.assertEqual(align.FIELD_MAP, before)


class Merging(unittest.TestCase):
    def test_researcher_fields_are_added_with_provenance(self):
        record = align_one({"bestmeta": {"dataset_title": "T", "hardware_and_acquisition": {"acquisition": {"x": 1}}}})
        self.assertEqual(record["bestmeta"]["dataset_title"], "T")
        self.assertEqual(acq(record, "x"), 1)
        self.assertEqual(acq(record, "video_resolution_width"), 320)
        self.assertEqual(record["provenance"]["dataset_title"], [{
            "source": "annotation", "source_path": "dataset_title", "method": "researcher_supplied",
            "value": "T", "selected": True}])
        self.assertEqual(record["conflicts"], [])

    def test_a_measured_value_wins_and_the_disagreement_is_recorded(self):
        record = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {"video_resolution_width": 640}}}})
        self.assertEqual(acq(record, "video_resolution_width"), 320)
        self.assertEqual(record["conflicts"], [{
            "path": "hardware_and_acquisition.acquisition.video_resolution_width", "extracted": 320, "annotation": 640}])
        evidence = record["provenance"]["hardware_and_acquisition.acquisition.video_resolution_width"]
        self.assertEqual([(e["source"], e["selected"]) for e in evidence], [("extracted", True), ("annotation", False)])
        self.assertIn("metadata_conflict", issue_codes(record))
        self.assertFalse(record["validation"]["valid"])

    def test_agreement_is_not_a_conflict_even_in_another_numeric_form(self):
        record = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {"video_resolution_width": 320.0, "bit_depth": 8.0}}}})
        self.assertEqual(record["conflicts"], [])

    def test_booleans_are_not_numbers(self):
        record = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {"bit_depth": True}}}},
                           ex=extraction(bit_depth=1))
        self.assertEqual(len(record["conflicts"]), 1)

    def test_playback_timing_and_generated_id_can_be_overridden_by_the_researcher(self):
        meta = {"dataset_id": "doi:10.1/x", "hardware_and_acquisition": {"acquisition": {
            "frame_rate": 1000, "recording_duration": "PT5S"}}}
        record = align_one({"bestmeta": meta})
        self.assertEqual(record["conflicts"], [])
        self.assertEqual((acq(record, "frame_rate"), acq(record, "recording_duration")), (1000, "PT5S"))
        self.assertEqual(record["bestmeta"]["dataset_id"], "doi:10.1/x")
        for path in ("dataset_id", "hardware_and_acquisition.acquisition.frame_rate"):
            self.assertEqual([(e["source"], e["selected"]) for e in record["provenance"][path]],
                             [("extracted", False), ("annotation", True)])

    def test_other_is_a_real_answer_so_a_specific_claim_still_conflicts(self):
        ex = extraction(codec="vp9", container="matroska,webm")
        agree = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {
            "video_codec": "other", "video_container_format": "other"}}}}, ex=ex)
        self.assertEqual(agree["conflicts"], [])
        disagree = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {"video_codec": "h264"}}}}, ex=ex)
        self.assertEqual([c["path"] for c in disagree["conflicts"]], [CODEC])

    def test_a_value_replacing_a_whole_measured_subtree_is_a_conflict(self):
        record = align_one({"bestmeta": {"hardware_and_acquisition": 5}})
        self.assertEqual(len(record["conflicts"]), 1)
        self.assertEqual(record["conflicts"][0]["annotation"], 5)
        self.assertIsInstance(record["bestmeta"]["hardware_and_acquisition"], dict)
        self.assertEqual(record["provenance"]["hardware_and_acquisition"][0]["method"], "aggregate_extracted_subtree")

    def test_a_dict_where_a_measured_leaf_stands_is_a_conflict(self):
        record = align_one({"bestmeta": {"hardware_and_acquisition": {"acquisition": {"video_resolution_width": {"v": 1}}}}})
        self.assertEqual(acq(record, "video_resolution_width"), 320)
        self.assertEqual(len(record["conflicts"]), 1)

    def test_empty_objects_and_lists_are_carried_over(self):
        record = align_one({"bestmeta": {"extra": {}, "list": [], "none": None}})
        self.assertEqual((record["bestmeta"]["extra"], record["bestmeta"]["list"], record["bestmeta"]["none"]), ({}, [], None))

    def test_annotations_pass_through_untouched(self):
        notes = {"well": "A1", "nested": {"k": [1, 2]}}
        record = align_one({"annotations": notes})
        self.assertEqual(record["annotations"], notes)
        self.assertIsNot(record["annotations"], notes)

    def test_inputs_are_never_modified_and_results_do_not_alias_them(self):
        ex = extraction(issues=[{"severity": "warning", "code": "w", "message": "m", "stage": "extract"}])
        meta = {"bestmeta": {"dataset_title": "T", "hardware_and_acquisition": {"acquisition": {"video_resolution_width": 640}}},
                "annotations": {"a": [1]}}
        before = copy.deepcopy((ex, meta))
        record = align.align_metadata(ex, meta, SCHEMA)
        self.assertEqual((ex, meta), before)
        record["extraction"]["raw"]["format"]["tags"]["major_brand"] = "changed"
        record["conflicts"][0]["annotation"] = "changed"
        record["annotations"]["a"].append(2)
        self.assertEqual((ex, meta), before)

    def test_alignment_is_deterministic(self):
        meta = {"bestmeta": VALID_METADATA}
        self.assertEqual(json.dumps(align_one(meta), sort_keys=True), json.dumps(align_one(meta), sort_keys=True))


class Validation(SchemaCase):
    def test_a_complete_record_is_valid(self):
        record = align_one({"bestmeta": VALID_METADATA})
        self.assertEqual(record["validation"], {
            "valid": True, "schema_valid": True, "schema_status": "available", "errors": [], "missing_required": []})

    def test_missing_required_lists_exactly_what_the_researcher_must_add(self):
        validation = align_one()["validation"]
        self.assertEqual(validation["missing_required"], [
            "dataset_license", "dataset_title", "hardware_and_acquisition.acquisition.recording_software_name",
            "hardware_and_acquisition.acquisition.recording_software_version", "hardware_and_acquisition.hardware"])
        self.assertFalse(validation["valid"])

    def test_diagnostics_have_no_noise_from_optional_alternatives(self):
        errors = align_one()["validation"]["errors"]
        self.assertTrue(errors)
        for error in errors:
            self.assertNotIn("'null'", error["message"])
            self.assertNotEqual((error["path"], error["code"]), ("hardware_and_acquisition", "type"))

    def test_errors_are_sorted_and_messages_are_bounded(self):
        meta = {"bestmeta": {"dataset_title": "x" * 3000, "hardware_and_acquisition": {"hardware": {"device_type": "x" * 3000}}}}
        errors = align_one(meta)["validation"]["errors"]
        self.assertEqual(errors, sorted(errors, key=lambda e: (e["path"], e["code"], e["message"])))
        self.assertTrue(all(len(e["message"]) <= 500 for e in errors))

    def researcher(self, top=None, hardware=None, acquisition=None):
        meta = copy.deepcopy(VALID_METADATA)
        meta.update(top or {})
        meta["hardware_and_acquisition"]["hardware"].update(hardware or {})
        meta["hardware_and_acquisition"]["acquisition"].update(acquisition or {})
        return [(e["path"].split(".")[-1], e["code"]) for e in align_one({"bestmeta": meta})["validation"]["errors"]]

    def test_enums_are_enforced_on_researcher_values(self):
        self.assertEqual(self.researcher(hardware={"field_of_view_unit": "parsec"}), [("field_of_view_unit", "enum")])

    def test_date_time_format_is_enforced(self):
        self.assertEqual(self.researcher(acquisition={"recording_start_datetime": "yesterday"}),
                         [("recording_start_datetime", "format")])
        self.assertEqual(self.researcher(acquisition={"recording_start_datetime": "2025-03-01T10:20:30Z"}), [])

    def test_uri_and_date_formats_are_enforced(self):
        self.assertEqual(self.researcher(top={"raw_data_repository_url": "not a uri"}), [("raw_data_repository_url", "format")])
        self.assertEqual(self.researcher(top={"raw_data_repository_url": "https://zenodo.org/records/1"}), [])
        self.assertEqual(self.researcher(top={"dataset_created_date": "2025-02-30"}), [("dataset_created_date", "format")])
        self.assertEqual(self.researcher(top={"dataset_created_date": "2025-02-28"}), [])

    def test_patterns_are_enforced(self):
        # playback timing may be overridden by the researcher, and the override is then validated
        self.assertEqual(self.researcher(acquisition={"recording_duration": "30 seconds"}), [("recording_duration", "pattern")])
        self.assertEqual(self.researcher(acquisition={"recording_duration": "PT30S"}), [])
        record = align_one({"bestmeta": {**VALID_METADATA}}, ex=extraction(duration_seconds=None))
        self.assertIn("recording_duration", " ".join(record["validation"]["missing_required"]))

    def test_unknown_keys_in_closed_classes_are_errors(self):
        meta = copy.deepcopy(VALID_METADATA)
        meta["hardware_and_acquisition"]["acquisition"]["not_a_field"] = 1
        codes = [e["code"] for e in align_one({"bestmeta": meta})["validation"]["errors"]]
        self.assertEqual(codes, ["additionalProperties"])

    def test_unknown_top_level_keys_are_allowed_by_the_published_schema(self):
        meta = {**VALID_METADATA, "my_own_field": 1}
        self.assertTrue(align_one({"bestmeta": meta})["validation"]["valid"])

    def test_lossless_codecs_must_state_their_bit_depth(self):
        record = align_one({"bestmeta": VALID_METADATA}, ex=extraction(codec="ffv1", bit_depth=None))
        self.assertEqual(record["validation"]["missing_required"], ["hardware_and_acquisition.acquisition.bit_depth"])
        self.assertTrue(align_one({"bestmeta": VALID_METADATA}, ex=extraction(codec="ffv1", bit_depth=10))["validation"]["valid"])

    def test_validity_needs_no_conflicts_and_no_error_issues(self):
        meta = {"bestmeta": VALID_METADATA}
        broken = {"severity": "error", "code": "ffprobe_failed", "message": "m", "stage": "extract"}
        self.assertFalse(align_one(meta, ex=extraction(issues=[broken]))["validation"]["valid"])
        warning = {**broken, "severity": "warning"}
        self.assertTrue(align_one(meta, ex=extraction(issues=[warning]))["validation"]["valid"])
        conflicting = copy.deepcopy(VALID_METADATA)
        conflicting["hardware_and_acquisition"]["acquisition"]["video_resolution_width"] = 641
        record = align_one({"bestmeta": conflicting})
        self.assertTrue(record["validation"]["schema_valid"])
        self.assertFalse(record["validation"]["valid"])

    def test_extraction_issues_are_carried_once(self):
        issue = {"severity": "warning", "code": "w", "message": "m", "stage": "extract"}
        record = align_one(ex=extraction(issues=[issue]))
        self.assertEqual(issue_codes(record).count("w"), 1)
        self.assertNotIn("issues", record["extraction"])
        self.assertIn("bestmeta_validation_failed", issue_codes(record))

    def test_without_a_schema_the_evidence_is_kept_and_the_record_is_a_draft(self):
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            record = align_one(schema=align.load_schema())
        self.assertEqual(acq(record, "video_resolution_width"), 320)
        validation = record["validation"]
        self.assertEqual((validation["valid"], validation["schema_valid"], validation["schema_status"]),
                         (False, None, "unavailable"))
        self.assertEqual([e["code"] for e in validation["errors"]], ["schema_unavailable"])
        self.assertIn("jsonschema_not_installed", issue_codes(record))

    def test_default_schema_is_loaded_when_none_is_given(self):
        record = align.align_metadata(extraction())
        self.assertEqual(record["validation"]["schema_status"], "available")

    def test_other_json_schemas_work_with_their_own_field_map(self):
        schema = {"type": "object", "required": ["image"], "properties": {"image": {
            "type": "object", "required": ["width_px"], "properties": {"width_px": {"type": "integer", "minimum": 1}}}}}
        context = self.custom_schema(schema)
        good = align_one(schema=context, field_map={"width": ("image", "width_px")})
        self.assertEqual(good["validation"], {
            "valid": True, "schema_valid": True, "schema_status": "available", "errors": [], "missing_required": []})
        self.assertEqual(good["bestmeta"]["image"], {"width_px": 320})
        bad = align_one(schema=context, field_map={"height": ("other", "h")})
        self.assertEqual(bad["validation"]["missing_required"], ["image"])


class RandomMetadata(SchemaCase):
    """Invariants of the merge for arbitrary researcher input (seeded, so failures reproduce)."""

    KEYS = ("hardware_and_acquisition", "acquisition", "hardware", "video_resolution_width", "frame_rate",
            "recording_duration", "dataset_id", "dataset_title", "bit_depth", "video_codec", "x", "a.b", "~", "")

    def scalar(self, rng):
        return rng.choice((0, 1, -7, 30, 30.0, 0.5, True, False, None, "", "text", "PT2S", "other", "h264", "ünï"))

    def mapping(self, rng, depth=0):
        result = {}
        for _ in range(rng.randint(0, 4)):
            kind = rng.random()
            if depth < 3 and kind < 0.4:
                result[rng.choice(self.KEYS)] = self.mapping(rng, depth + 1)
            elif kind < 0.5:
                result[rng.choice(self.KEYS)] = [self.scalar(rng) for _ in range(rng.randint(0, 3))]
            else:
                result[rng.choice(self.KEYS)] = self.scalar(rng)
        return result

    def leaves(self, node, prefix=()):
        for key, value in node.items():
            if isinstance(value, dict):
                yield from self.leaves(value, (*prefix, key))
            else:
                yield (*prefix, key), value

    def assert_researcher_values_landed(self, supplied, final, conflicts, prefix=()):
        for key, value in supplied.items():
            path = align._path((*prefix, key))
            if path in conflicts:
                continue
            if isinstance(value, dict) and isinstance(final.get(key), dict):
                self.assert_researcher_values_landed(value, final[key], conflicts, (*prefix, key))
            else:
                self.assertTrue(align._same_json(final[key], value), path)

    def test_arbitrary_researcher_metadata_keeps_every_invariant(self):
        rng = random.Random(20261104)
        for case in range(400):
            metadata = {"bestmeta": self.mapping(rng), "annotations": self.mapping(rng)}
            ex = extraction()
            before = copy.deepcopy((ex, metadata))
            with self.subTest(case=case):
                record = align.align_metadata(ex, metadata, SCHEMA)
                self.assertEqual((ex, metadata), before, "inputs must never be modified")
                text = json.dumps(record, allow_nan=False, sort_keys=True)  # strictly serialisable
                self.assertEqual(text, json.dumps(align.align_metadata(ex, metadata, SCHEMA), sort_keys=True))
                conflicts = {c["path"] for c in record["conflicts"]}
                replaceable = {"dataset_id", "hardware_and_acquisition.acquisition.frame_rate",
                               "hardware_and_acquisition.acquisition.recording_duration"}
                self.assertFalse(conflicts & replaceable)
                self.assert_researcher_values_landed(metadata["bestmeta"], record["bestmeta"], conflicts)
                for conflict in record["conflicts"]:
                    self.assertIn(conflict["path"], record["provenance"])
                    self.assertNotEqual(conflict["extracted"], conflict["annotation"])
                for parts, _ in self.leaves(record["bestmeta"]):
                    # a researcher object that replaces a whole value is one provenance entry, at the replaced path
                    covered = any(align._path(parts[:n]) in record["provenance"] for n in range(1, len(parts) + 1))
                    self.assertTrue(covered, f"{align._path(parts)} must say where it came from")
                validation = record["validation"]
                if validation["valid"]:
                    self.assertTrue(validation["schema_valid"] and not conflicts)
                self.assertEqual(validation["schema_valid"], not validation["errors"])


class ErrorFlattening(unittest.TestCase):
    def errors(self, schema, instance):
        from jsonschema import Draft202012Validator
        return [leaf for e in Draft202012Validator(schema).iter_errors(instance) for leaf in align._leaves(e)]

    def test_null_alternatives_of_optional_properties_are_dropped(self):
        schema = {"properties": {"x": {"anyOf": [{"type": "object", "required": ["a", "b"]}, {"type": "null"}]}}}
        leaves = self.errors(schema, {"x": {}})
        self.assertEqual(sorted(e.validator for e in leaves), ["required", "required"])

    def test_pure_kind_mismatches_are_all_kept(self):
        schema = {"properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
        self.assertEqual(len(self.errors(schema, {"x": 5})), 2)

    def test_plain_errors_pass_through(self):
        self.assertEqual([e.validator for e in self.errors({"type": "object", "required": ["a"]}, {})], ["required"])


if __name__ == "__main__":
    unittest.main()

import csv
import io
import json
import os
import unittest
from unittest import mock

import export
from tests.support import TempDirCase


def record(name="a.mp4", valid=True, issues=(), missing=(), bestmeta=None, technical=None, raw=None, annotations=None):
    return {
        "extraction": {
            "asset": {"relative_path": name, "sha256": "0" * 64, "path": "/" + name, "size_bytes": 1, "mtime_ns": 1},
            "raw": raw or {},
            "technical": technical or {},
        },
        "bestmeta": bestmeta or {},
        "annotations": annotations or {},
        "provenance": {},
        "conflicts": [],
        "issues": list(issues),
        "validation": {
            "valid": valid, "schema_valid": valid, "schema_status": "available", "errors": [],
            "missing_required": list(missing),
        },
    }


def issue(severity="warning", code="c"):
    return {"severity": severity, "code": code, "message": "m", "stage": "x"}


class ExportCase(TempDirCase):
    def export(self, records, name="out", import_issues=None, **options):
        issues = [] if import_issues is None else import_issues
        return export.export_metadata(
            records, self.out(name), run={"tool": "t"}, schema={"status": "available"}, import_issues=issues, **options
        )

    def load_json(self, name="out"):
        return json.loads((self.out(name) / "metadata.json").read_text(encoding="utf-8"))

    def load_csv(self, name="out"):
        text = (self.out(name) / "metadata.csv").read_bytes().decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text, newline="")))


class Publication(ExportCase):
    def test_a_successful_run_leaves_exactly_the_two_documented_files(self):
        self.export([record()])
        self.assertEqual(sorted(p.name for p in self.out().iterdir()), ["metadata.csv", "metadata.json"])

    def test_document_layout_and_summary(self):
        records = [record("a.mp4", missing=["x", "y"], valid=False), record("b.mp4"), record("c.mp4", valid=False, missing=["x"],
                                                                                           issues=[issue("error")])]
        summary = self.export(records, import_issues=[issue("error", "import")])
        document = self.load_json()
        self.assertEqual(list(document), ["run", "schema", "records", "import_issues", "summary"])
        self.assertEqual(document["summary"], summary)
        self.assertEqual(summary, {"files": 3, "valid": 1, "draft": 2, "errors": 2, "success": False,
                                   "missing_required": {"x": 2, "y": 1}})
        self.assertEqual([r["extraction"]["asset"]["relative_path"] for r in document["records"]], ["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual(document["run"], {"tool": "t"})

    def test_missing_required_is_ordered_by_frequency_then_name(self):
        summary = self.export([record(missing=["b", "a"]), record(missing=["b"]), record(missing=["c"])])
        self.assertEqual(list(summary["missing_required"].items()), [("b", 2), ("a", 1), ("c", 1)])

    def test_success_needs_all_valid_and_no_errors_anywhere(self):
        self.assertTrue(self.export([record(), record("b.mp4")], name="ok")["success"])
        self.assertFalse(self.export([record(issues=[issue("error")])], name="e1")["success"])
        self.assertFalse(self.export([record()], name="e2", import_issues=[issue("error")])["success"])
        self.assertTrue(self.export([record()], name="w", import_issues=[issue("warning")])["success"])

    def test_import_issues_gathered_while_records_stream_are_included(self):
        late = []

        def stream():
            yield record("a.mp4")
            late.append(issue("warning", "found_late"))
            yield record("b.mp4")

        self.export(stream(), import_issues=late)
        self.assertEqual([i["code"] for i in self.load_json()["import_issues"]], ["found_late"])

    def test_no_records_gives_valid_documents_and_no_success(self):
        summary = self.export([])
        self.assertEqual(summary["files"], 0)
        self.assertFalse(summary["success"])
        self.assertEqual(self.load_json()["records"], [])
        self.assertEqual(self.load_csv(), [])
        header = (self.out() / "metadata.csv").read_bytes().decode("utf-8-sig").strip()
        self.assertEqual(header, "file,sha256,valid")

    def test_output_is_deterministic(self):
        records = [record("a.mp4", bestmeta={"z": 1, "a": {"y": [1, 2], "b": None}}), record("b.mp4")]
        self.export(records, name="one")
        self.export(records, name="two")
        for name in ("metadata.json", "metadata.csv"):
            self.assertEqual((self.out("one") / name).read_bytes(), (self.out("two") / name).read_bytes())

    def test_parent_folders_are_created(self):
        target = self.tmp / "deep" / "er" / "out"
        export.export_metadata([record()], target, run={}, schema={}, import_issues=[])
        self.assertTrue((target / "metadata.json").is_file())

    def test_scales_to_many_records(self):
        summary = self.export(record(f"f{i}.mp4", technical={"width": i}) for i in range(3000))
        self.assertEqual(summary["files"], 3000)
        self.assertEqual(len(self.load_csv()), 3000)


class NoOverwrite(ExportCase):
    def test_an_existing_folder_is_refused_and_left_alone(self):
        self.out().mkdir()
        (self.out() / "keep.txt").write_text("mine")
        with self.assertRaises(FileExistsError):
            self.export([record()])
        self.assertEqual([p.name for p in self.out().iterdir()], ["keep.txt"])

    def test_an_existing_file_is_refused_and_left_alone(self):
        self.out().write_text("mine")
        with self.assertRaises(FileExistsError):
            self.export([record()])
        self.assertEqual(self.out().read_text(), "mine")

    def test_records_are_not_consumed_when_the_target_exists(self):
        self.out().mkdir()
        consumed = []

        def stream():
            consumed.append(1)
            yield record()

        with self.assertRaises(FileExistsError):
            self.export(stream())
        self.assertEqual(consumed, [])


class Cleanup(ExportCase):
    def assert_nothing_left(self):
        self.assertFalse(self.out().exists())

    def test_error_while_producing_records(self):
        def stream():
            yield record()
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.export(stream())
        self.assert_nothing_left()

    def test_interrupt_while_producing_records(self):
        def stream():
            yield record()
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.export(stream())
        self.assert_nothing_left()

    def test_non_finite_numbers_are_refused(self):
        with self.assertRaises(ValueError):
            self.export([record(bestmeta={"x": float("nan")})])
        self.assert_nothing_left()

    def test_values_that_are_not_json_are_refused(self):
        with self.assertRaises(TypeError):
            self.export([record(bestmeta={"x": {1, 2}})])
        self.assert_nothing_left()

    def test_failure_at_the_final_step_leaves_no_partial_run(self):
        real = os.replace
        calls = []

        def failing(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(source, destination)

        with mock.patch.object(export.os, "replace", failing):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.export([record()])
        self.assert_nothing_left()

    def test_the_json_document_is_published_last(self):
        order = []
        real = os.replace

        def spy(source, destination):
            order.append(os.path.basename(destination))
            return real(source, destination)

        with mock.patch.object(export.os, "replace", spy):
            self.export([record()])
        self.assertEqual(order, ["metadata.csv", "metadata.json"])


class JsonDocument(ExportCase):
    def test_ascii_only_and_lossless_for_any_file_name(self):
        names = ["ünï 视频.mp4", "bad\udcff.mp4", "emoji \U0001f3a5.mp4", 'quote".mp4']
        self.export([record(n) for n in names])
        raw = (self.out() / "metadata.json").read_bytes()
        raw.decode("ascii")
        self.assertEqual([r["extraction"]["asset"]["relative_path"] for r in self.load_json()["records"]], names)

    def test_keys_are_sorted_inside_records(self):
        self.export([record(bestmeta={"b": 1, "a": 2})])
        text = (self.out() / "metadata.json").read_text()
        self.assertLess(text.index('"a": 2'), text.index('"b": 1'))

    def test_pretty_printed_for_people(self):
        self.export([record()])
        self.assertGreater((self.out() / "metadata.json").read_text().count("\n"), 20)


class CsvView(ExportCase):
    def test_bom_header_order_and_rectangular_rows(self):
        self.export([record("a.mp4", bestmeta={"z": 1}, technical={"w": 2}), record("b.mp4", bestmeta={"y": 3})])
        data = (self.out() / "metadata.csv").read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        rows = self.load_csv()
        self.assertEqual(list(rows[0])[:3], ["file", "sha256", "valid"])
        self.assertEqual(list(rows[0])[3:], sorted(list(rows[0])[3:]))
        self.assertEqual([r["file"] for r in rows], ["a.mp4", "b.mp4"])
        self.assertEqual((rows[0]["bestmeta.z"], rows[0]["bestmeta.y"], rows[1]["bestmeta.y"]), ("1", "", "3"))
        self.assertEqual(rows[0]["valid"], "True")

    def test_nested_values_flatten_and_collections_are_json_cells(self):
        self.export([record(bestmeta={"a": {"b": {"c": 1}}, "list": [1, {"x": 2}], "empty": {}, "none": None},
                            raw={"streams": [{"i": 1}]}, annotations={"note": "n"})])
        row = self.load_csv()[0]
        self.assertEqual(row["bestmeta.a.b.c"], "1")
        self.assertEqual(json.loads(row["bestmeta.list"]), [1, {"x": 2}])
        self.assertEqual(row["bestmeta.empty"], "{}")
        self.assertEqual(row["bestmeta.none"], "")
        self.assertEqual(json.loads(row["raw.streams"]), [{"i": 1}])
        self.assertEqual(row["annotations.note"], "n")

    def test_dots_and_tildes_in_keys_cannot_collide_with_nesting(self):
        self.export([record(bestmeta={"a.b": 1, "a": {"b": 2}, "t~x": 3})])
        row = self.load_csv()[0]
        self.assertEqual((row["bestmeta.a~1b"], row["bestmeta.a.b"], row["bestmeta.t~0x"]), ("1", "2", "3"))

    def test_spreadsheet_formulas_are_neutralised(self):
        attacks = ["=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1)", " =1", "\t=1", "\r=1", "\n=1", "\ufeff=1", " \t+x"]
        self.export([record(bestmeta={f"k{i}": value for i, value in enumerate(attacks)})])
        row = self.load_csv()[0]
        for i, value in enumerate(attacks):
            self.assertTrue(row[f"bestmeta.k{i}"].startswith("'"), repr(value))

    def test_cell_helper_leaves_ordinary_values_alone(self):
        for value in ("normal", "a=b", "3-4", "x+y", "", None, 5, -5, 1.5, True, "ünï"):
            self.assertEqual(export._csv_cell(value), value)
        self.assertEqual(export._csv_cell("-5"), "'-5")

    def test_unencodable_text_is_written_readably(self):
        self.export([record("bad\udcff.mp4")])
        self.assertIn("bad\\udcff.mp4", self.load_csv()[0]["file"])

    def test_columns_are_the_union_over_all_records(self):
        self.export([record("a.mp4", technical={"only_a": 1}), record("b.mp4", technical={"only_b": 2})])
        rows = self.load_csv()
        self.assertEqual((rows[0]["technical.only_a"], rows[1]["technical.only_a"]), ("1", ""))
        self.assertEqual((rows[0]["technical.only_b"], rows[1]["technical.only_b"]), ("", "2"))


if __name__ == "__main__":
    unittest.main()

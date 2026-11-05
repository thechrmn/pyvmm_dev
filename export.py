"""Stage 4 - write the complete JSON record and a spreadsheet view into a new folder."""

import csv
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path


def _json(value, indent=None):
    """Canonical JSON: ASCII-only (lossless for any file name), sorted keys, no NaN."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=indent)


def _nest(value, depth):
    """Pretty JSON for a value placed ``depth`` spaces deep inside the output document."""
    return _json(value, indent=2).replace("\n", "\n" + " " * depth)


def _flatten(value, prefix, row):
    for key, child in value.items():
        name = prefix + key.replace("~", "~0").replace(".", "~1")
        if isinstance(child, dict) and child:
            _flatten(child, name + ".", row)
        else:
            row[name] = _json(child) if isinstance(child, (dict, list)) else child


def _row(record):
    asset = record["extraction"]["asset"]
    row = {
        "file": asset["relative_path"],
        "sha256": asset["sha256"],
        "valid": record["validation"]["valid"],
    }
    for name, value in (
        ("bestmeta", record["bestmeta"]),
        ("technical", record["extraction"]["technical"]),
        ("raw", record["extraction"]["raw"]),
        ("annotations", record["annotations"]),
    ):
        _flatten(value, name + ".", row)
    return row


def _csv_cell(value):
    """Neutralise spreadsheet formulas: a leading = + - @ (or control character) gets a quote."""
    if isinstance(value, str) and (
        value.lstrip(" \t\r\n\v\f\ufeff").startswith(("=", "+", "-", "@"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def _sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def _write(target, records, run, schema, import_issues):
    json_tmp, csv_tmp = target / ".metadata.json.tmp", target / ".metadata.csv.tmp"
    files = valid = errors = 0
    missing = Counter()
    columns = {"file", "sha256", "valid"}
    # Rows are spooled as JSON text so memory stays flat however many records arrive.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as rows:
        with json_tmp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(f'{{\n  "run": {_nest(run, 2)},\n  "schema": {_nest(schema, 2)},\n  "records": [')
            for record in records:
                stream.write(("," if files else "") + "\n    " + _nest(record, 4))
                files += 1
                valid += record["validation"]["valid"]
                errors += sum(issue["severity"] == "error" for issue in record["issues"])
                missing.update(record["validation"]["missing_required"])
                row = _row(record)
                columns.update(row)
                rows.write(_json(row) + "\n")
            errors += sum(issue["severity"] == "error" for issue in import_issues)
            summary = {
                "files": files,
                "valid": valid,
                "draft": files - valid,
                "errors": errors,
                "success": bool(files) and valid == files and not errors,
                "missing_required": dict(sorted(missing.items(), key=lambda p: (-p[1], p[0]))),
            }
            stream.write(("\n  ]" if files else "]") + f',\n  "import_issues": {_nest(import_issues, 2)}')
            stream.write(f',\n  "summary": {_nest(summary, 2)}\n}}\n')
            _sync(stream)
        rows.seek(0)
        first = ["file", "sha256", "valid"]
        with csv_tmp.open("x", encoding="utf-8-sig", errors="backslashreplace", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=first + sorted(columns.difference(first)))
            writer.writeheader()
            for line in rows:
                writer.writerow({key: _csv_cell(value) for key, value in json.loads(line).items()})
            _sync(stream)
    csv_tmp.replace(target / "metadata.csv")
    json_tmp.replace(target / "metadata.json")  # last: its presence marks a complete run
    return summary


def export_metadata(records, output, *, run, schema, import_issues):
    """Consume ``records`` once and write ``metadata.json`` and ``metadata.csv`` to ``output``.

    ``output`` must not exist: it is created here (never reused or overwritten) and removed
    again if anything fails, so a folder that exists afterwards holds a complete run.
    ``import_issues`` is read after the last record, so it may still be growing while
    ``records`` is consumed. Returns the summary dictionary.
    """
    target = Path(output).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    try:
        return _write(target, records, run, schema, import_issues)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise

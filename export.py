"""Stream complete metadata and its spreadsheet view into a new directory."""

import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


def _flatten(value, prefix, row):
    for key, child in value.items():
        name = prefix + key.replace("~", "~0").replace(".", "~1")
        if isinstance(child, dict) and child:
            _flatten(child, name + ".", row)
        else:
            row[name] = _json(child) if isinstance(child, (dict, list)) else child


def _row(record):
    extraction = record["extraction"]
    row = {
        "file": extraction["asset"]["relative_path"],
        "sha256": extraction["asset"]["sha256"],
        "valid": record["validation"]["valid"],
    }
    for name, value in (
        ("bestmeta", record["bestmeta"]),
        ("technical", extraction["technical"]),
        ("raw", extraction["raw"]),
        ("annotations", record["annotations"]),
    ):
        _flatten(value, name + ".", row)
    return row


def _csv_cell(value):
    if isinstance(value, str) and (
        value.lstrip(" \t\r\n\v\f\ufeff").startswith(("=", "+", "-", "@"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def _sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def _publish(source, target):
    """Publish atomically without replacing even a concurrently created empty directory."""
    if os.name == "nt":
        source.rename(target)
        return
    import ctypes

    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError("Atomic no-replace publication is unavailable on this OS.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def export_metadata(records, output, *, import_issues=None, schema_info=None, run_info=None):
    """Consume records once and publish both outputs together; roll back on failure.

    Return counts (files, valid, draft, errors) and the boolean success status.
    """
    requested = Path(output).expanduser().absolute()
    if "\0" in os.fspath(requested):
        raise ValueError("Output path contains a null byte.")
    if os.path.lexists(requested):
        raise FileExistsError(f"Output already exists: {requested}")
    requested.parent.mkdir(parents=True, exist_ok=True)
    target = requested.parent.resolve(strict=True) / requested.name
    if os.path.lexists(target):
        raise FileExistsError(f"Output already exists: {target}")
    staging = Path(tempfile.mkdtemp(prefix=".pyvmm-", dir=target.parent)).resolve()
    try:
        issues = [] if import_issues is None else import_issues
        summary = {
            "files": 0,
            "valid": 0,
            "errors": sum(issue["severity"] == "error" for issue in issues),
        }
        columns = {"file", "sha256", "valid"}
        header = {"schema": schema_info, "run": run_info, "import_issues": issues}
        # Text JSON rows avoid retaining a whole batch or accepting binary object data.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as rows:
            with (staging / "metadata.json").open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(_json(header)[:-1] + ', "records": [\n')
                for record in records:
                    if summary["files"]:
                        stream.write(",\n")
                    stream.write(_json(record))
                    summary["files"] += 1
                    summary["valid"] += record["validation"]["valid"]
                    summary["errors"] += sum(i["severity"] == "error" for i in record["issues"])
                    row = _row(record)
                    columns.update(row)
                    rows.write(_json(row) + "\n")
                    del record, row
                summary["draft"] = summary["files"] - summary["valid"]
                summary["success"] = (
                    bool(summary["files"]) and not summary["draft"] and not summary["errors"]
                )
                stream.write('\n], "summary": ' + _json(summary) + "}\n")
                _sync(stream)
            rows.seek(0)
            with (staging / "metadata.csv").open(
                "w", encoding="utf-8-sig", errors="backslashreplace", newline=""
            ) as stream:
                first = ["file", "sha256", "valid"]
                writer = csv.DictWriter(
                    stream, fieldnames=first + sorted(columns.difference(first))
                )
                writer.writeheader()
                for line in rows:
                    writer.writerow(
                        {key: _csv_cell(value) for key, value in json.loads(line).items()}
                    )
                _sync(stream)
        _publish(staging, target)
        return summary
    finally:
        active_error = sys.exc_info()[1]
        try:
            if staging.parent == target.parent and staging.exists():
                shutil.rmtree(staging)
        except OSError as cleanup_error:
            if active_error is None:
                raise
            active_error.add_note(f"Could not remove staging directory {staging}: {cleanup_error}")

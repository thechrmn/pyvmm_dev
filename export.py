"""Write the complete metadata and a spreadsheet view into a new folder."""

import csv
import json
import os
import pickle
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def _json(value):
    try:
        # Standard JSON escapes preserve Unicode and surrogate filenames without
        # extra encoded copies; JSON remains the lossless representation.
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Metadata is not finite JSON: {error}") from error


def _flatten(value, prefix, result):
    for key, child in value.items():
        name = prefix + str(key).replace("~", "~0").replace(".", "~1")
        if isinstance(child, dict) and child:
            _flatten(child, name + ".", result)
        else:
            result[name] = _json(child) if isinstance(child, (dict, list)) else child


def _row(record):
    asset = record["extraction"]["asset"]
    row = {
        "file": asset["relative_path"],
        "sha256": asset["sha256"],
        "valid": record["validation"]["valid"],
    }
    _flatten(record["bestmeta"], "bestmeta.", row)
    _flatten(record["extraction"]["technical"], "technical.", row)
    _flatten(record.get("annotations", {}), "annotations.", row)
    return row


def _csv_cell(value):
    if isinstance(value, str) and (
        value.lstrip(" \t\r\n\v\f\ufeff").startswith(("=", "+", "-", "@"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value
    return value


def _typed(value, kind, label):
    if not isinstance(value, kind):
        expected = {dict: "an object", list: "a list", bool: "a boolean"}[kind]
        raise ValueError(f"{label} must be {expected}.")
    return value


def _checked_record(record):
    """Reject malformed records and contradictory claims of validity."""
    record = _typed(record, dict, "Record")
    extraction = _typed(record.get("extraction"), dict, "Record extraction")
    asset = _typed(extraction.get("asset"), dict, "Imported asset")
    if not all(isinstance(asset.get(k), str) and asset[k] for k in ("relative_path", "sha256")):
        raise ValueError("An imported asset must contain a source path and SHA-256.")
    if len(asset["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in asset["sha256"]):
        raise ValueError("An imported asset must contain a lowercase hexadecimal SHA-256.")
    for name, value in {
        "bestmeta": record.get("bestmeta"),
        "technical": extraction.get("technical"),
        "raw": extraction.get("raw"),
        "provenance": record.get("provenance"),
        "annotations": record.get("annotations", {}),
    }.items():
        _typed(value, dict, f"Record {name}")
    validation = _typed(record.get("validation"), dict, "Record validation")
    valid = _typed(validation.get("valid"), bool, "Record validation.valid")
    errors = _typed(validation.get("errors"), list, "Record validation.errors")
    conflicts = _typed(record.get("conflicts"), list, "Record conflicts")
    for conflict in conflicts:
        _typed(conflict, dict, "Conflict")
    issues = record.get("issues")
    extraction_issues = extraction.get("issues", [])
    _checked_issues(issues)
    _checked_issues(extraction_issues)
    if valid and (
        errors
        or any(i["severity"] == "error" for group in (issues, extraction_issues) for i in group)
        or conflicts
    ):
        raise ValueError("A record with validation or extraction errors cannot be valid.")
    return record


def _checked_issues(issues):
    if not isinstance(issues, list) or any(
        not isinstance(i, dict)
        or i.get("severity") not in ("error", "warning", "info")
        or not isinstance(i.get("code"), str)
        or not isinstance(i.get("message"), str)
        for i in issues
    ):
        raise ValueError("Issues must be objects containing severity, code, and message.")


def _sync(stream) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _write_json(path, records, rows, issues, schema_info, run_info):
    summary = {
        "files": 0,
        "valid": 0,
        "draft": 0,
        "errors": sum(i["severity"] == "error" for i in issues),
    }
    columns = {"file", "sha256", "valid"}
    header = {"schema": schema_info, "run": run_info, "import_issues": issues}
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        # Append a streamed records array to the small serialized header.
        stream.write(_json(header)[:-1] + ', "records": [\n')
        for record in records:
            _checked_record(record)
            if summary["files"]:
                stream.write(",\n")
            stream.write(_json(record))
            summary["files"] += 1
            summary["valid"] += record["validation"]["valid"]
            summary["errors"] += sum(i["severity"] == "error" for i in record["issues"])
            row = _row(record)
            columns.update(row)
            # Only our JSON-checked rows enter this private temporary stream.
            pickle.dump(row, rows, protocol=5)
            del record, row  # Release buffers before the iterator probes the next file.
        summary["draft"] = summary["files"] - summary["valid"]
        summary["success"] = (
            bool(summary["files"]) and not summary["draft"] and not summary["errors"]
        )
        stream.write('\n], "summary": ' + _json(summary) + "}\n")
        _sync(stream)
    return summary, columns


def _write_csv(path, rows, columns, count) -> None:
    first = ["file", "sha256", "valid"]
    rows.seek(0)
    with path.open("w", encoding="utf-8-sig", errors="backslashreplace", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=first + sorted(columns.difference(first)))
        writer.writeheader()
        for _ in range(count):
            try:
                row = _typed(pickle.load(rows), dict, "Temporary CSV row")
            except (EOFError, pickle.UnpicklingError) as error:
                raise ValueError("Could not read the temporary CSV row spool.") from error
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})
            del row
        _sync(stream)


def _publish(source, target):
    """Atomically reject existing destinations, including concurrent empty ones."""
    source_bytes, target_bytes = os.fsencode(source), os.fsencode(target)
    if b"\0" in source_bytes or b"\0" in target_bytes:
        raise ValueError("Publication paths must not contain null bytes.")
    if os.name == "nt":
        source.rename(target)  # Windows rename never replaces an existing path.
        return
    import ctypes

    # A plain POSIX rename can replace an empty directory after an exists check.
    # Linux renameat2(RENAME_NOREPLACE) closes that race; fail rather than weaken it.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError("Atomic no-replace directory publication is unavailable on this OS.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, source_bytes, -100, target_bytes, 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(target))


@contextmanager
def _staged_output(output):
    requested = Path(output).expanduser().absolute()
    if "\0" in os.fspath(requested):
        raise ValueError("Output path must not contain null bytes.")
    if os.path.lexists(requested):
        raise FileExistsError(f"Output already exists: {requested}")
    requested.parent.mkdir(parents=True, exist_ok=True)
    target = requested.parent.resolve(strict=True) / requested.name
    if os.path.lexists(target):
        raise FileExistsError(f"Output already exists: {target}")
    staging = Path(tempfile.mkdtemp(prefix=".pyvmm-", dir=target.parent)).resolve()
    try:
        yield staging
        _publish(staging, target)
    finally:
        if staging.exists() and staging.parent == target.parent:
            active_error = sys.exc_info()[1]
            try:
                shutil.rmtree(staging)
            except OSError as cleanup_error:
                if active_error is None:
                    raise
                active_error.add_note(
                    f"Could not remove staging directory {staging}: {cleanup_error}"
                )


def export_metadata(records, output, *, import_issues=None, schema_info=None, run_info=None):
    """Stream JSON/CSV and publish only after both files are complete."""
    issues = [] if import_issues is None else import_issues
    _checked_issues(issues)
    with (
        _staged_output(output) as staging,
        tempfile.TemporaryFile() as rows,
    ):
        summary, columns = _write_json(
            staging / "metadata.json", records, rows, issues, schema_info, run_info
        )
        _write_csv(staging / "metadata.csv", rows, columns, summary["files"])
    return summary

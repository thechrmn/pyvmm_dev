"""Run import -> extraction -> BeStMeta alignment -> JSON/CSV export."""

__version__ = "0.1.0.dev0"

import argparse
import hashlib
import importlib
import json
import os
import platform
import sys
from datetime import UTC, datetime
from importlib import metadata as package_metadata
from pathlib import Path

RUNTIME_DISTRIBUTIONS = (
    "attrs",
    "jsonschema",
    "jsonschema-specifications",
    "referencing",
    "rfc3339-validator",
    "rfc3986-validator",
    "rpds-py",
    "six",
    "typing-extensions",
)

try:
    from jsonschema.exceptions import SchemaError

    _prefix = f"{__package__}." if __package__ else ""
    _alignment = importlib.import_module(_prefix + "align")
    align_metadata = _alignment.align_metadata
    read_json = _alignment.read_json
    schema_provenance = _alignment.schema_provenance
    validate_annotations = _alignment.validate_annotations
    export_metadata = importlib.import_module(_prefix + "export").export_metadata
    _extraction = importlib.import_module(_prefix + "extract")
    extract_metadata = _extraction.extract_metadata
    inspect_ffprobe = _extraction.inspect_ffprobe
    import_files = importlib.import_module(_prefix + "import").import_files
except ImportError as error:
    _dependency_problem = str(error)
else:
    _dependency_problem = None


def main(argv: list[str] | None = None) -> int:
    """Run the configured pipeline and return its documented exit status."""
    parser = argparse.ArgumentParser(
        description="Extract video metadata and align it to BeStMeta."
    )
    parser.add_argument("--version", action="version", version=f"PyVMM {__version__}")
    parser.add_argument("inputs", nargs="+", type=Path, help="Video files or folders")
    parser.add_argument("-o", "--output", type=Path, required=True, help="New output folder")
    parser.add_argument("--metadata", type=Path, help="Optional BeStMeta metadata JSON")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable")
    args = parser.parse_args(argv)
    if _dependency_problem:
        print(
            f"Error: {_dependency_problem}. Install requirements.txt and check the project files.",
            file=sys.stderr,
        )
        return 2
    try:
        if os.path.lexists(args.output.expanduser()):
            raise FileExistsError("Choose a new output folder.")
        # Complete configuration checks before hashing or probing recordings.
        schema_info = schema_provenance()
        metadata_path = args.metadata.expanduser() if args.metadata else None
        metadata = read_json(metadata_path) if metadata_path else {}
        annotation_hash = hashlib.sha256(
            json.dumps(metadata, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        metadata = validate_annotations(metadata)
        probe_info = inspect_ffprobe(args.ffprobe)
        tool = probe_info["executable"]
        run_info = {
            "software": {"name": "PyVMM", "version": __version__},
            "started_at_utc": datetime.now(UTC).isoformat(),
            "arguments": list(sys.argv[1:] if argv is None else argv),
            "pipeline_sources": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ("main.py", "import.py", "extract.py", "align.py", "export.py")
            },
            "python": platform.python_version(),
            "environment": {
                "python_implementation": platform.python_implementation(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "libraries": {name: package_metadata.version(name) for name in RUNTIME_DISTRIBUTIONS},
            "runtime_lock_sha256": hashlib.sha256(
                Path(__file__).with_name("requirements.txt").read_bytes()
            ).hexdigest(),
            "ffprobe": probe_info,
            "annotation_input": {
                "path": str(metadata_path.resolve()) if metadata_path else None,
                "canonical_json_sha256": annotation_hash,
            },
        }
        assets, issues = import_files(args.inputs, exclude_paths=[args.output])
        records = (
            align_metadata(extract_metadata(asset, ffprobe=tool), metadata) for asset in assets
        )
        summary = export_metadata(
            records,
            args.output,
            import_issues=issues,
            schema_info=schema_info,
            run_info=run_info,
        )
        print(json.dumps(summary))
        return 0 if summary["success"] else 1
    except (
        KeyboardInterrupt,
        OSError,
        ValueError,
        RuntimeError,
        SchemaError,
        package_metadata.PackageNotFoundError,
    ) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        print("Interrupted." if interrupted else f"Error: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 130 if interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())

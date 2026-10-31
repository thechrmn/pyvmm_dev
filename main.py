"""Run import -> extraction -> BeStMeta alignment -> JSON/CSV export."""

import argparse
import hashlib
import importlib
import json
import os
import platform
import sys
from importlib import metadata as package_metadata
from pathlib import Path

try:
    from jsonschema.exceptions import SchemaError

    from align import (
        align_metadata,
        read_json,
        schema_provenance,
        validate_annotations,
    )
    from export import export_metadata
    from extract import extract_metadata, inspect_ffprobe

    import_files = importlib.import_module("import").import_files
except ImportError as error:
    _dependency_problem = str(error)
else:
    _dependency_problem = None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract video metadata and align it to BeStMeta.")
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
        assets, issues = import_files(args.inputs, exclude_paths=[args.output])

        records = (
            align_metadata(extract_metadata(asset, ffprobe=tool), metadata) for asset in assets
        )

        run_info = {
            "python": platform.python_version(),
            "libraries": {
                name: package_metadata.version(name)
                for name in ("jsonschema", "rfc3339-validator", "rfc3986-validator")
            },
            "ffprobe": probe_info,
            "annotation_input": {
                "path": str(metadata_path.resolve()) if metadata_path else None,
                "canonical_json_sha256": annotation_hash,
            },
        }
        summary = export_metadata(
            records,
            args.output,
            import_issues=issues,
            schema_info=schema_info,
            run_info=run_info,
        )
        print(json.dumps(summary))
        return 0 if summary["success"] else 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, SchemaError) as error:
        print(f"Error: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

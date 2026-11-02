"""Run import -> extraction -> optional alignment/validation -> JSON/CSV export."""

__version__ = "0.1.0.dev0"

import argparse
import importlib
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Run the configured pipeline and return its documented exit status."""
    parser = argparse.ArgumentParser(
        description="Extract video metadata; optionally map and validate it."
    )
    parser.add_argument("--version", action="version", version=f"PyVMM {__version__}")
    parser.add_argument("inputs", nargs="+", type=Path, help="Video files or folders")
    parser.add_argument("-o", "--output", type=Path, required=True, help="New output folder")
    parser.add_argument("--metadata", type=Path, help="Optional researcher metadata JSON object")
    alignment = parser.add_mutually_exclusive_group()
    alignment.add_argument("--bestmeta", action="store_true", help="Use the BeStMeta field adapter")
    alignment.add_argument(
        "--mapping", type=Path, help="JSON file of source-to-target JSON Pointers"
    )
    parser.add_argument(
        "--schema", type=Path, help="Optional local JSON Schema; unavailable schemas export drafts"
    )
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable")
    parser.add_argument("--timeout", type=float, help="Per-file probe timeout in seconds")
    parser.add_argument("--max-output-bytes", type=int, help="Per-file combined probe output limit")
    parser.add_argument(
        "--extensions", help="Comma-separated folder extensions; * includes all files"
    )
    args = parser.parse_args(argv)
    probe_options = {
        key: getattr(args, key)
        for key in ("timeout", "max_output_bytes")
        if getattr(args, key) is not None
    }
    if any(not 0 < value < float("inf") for value in probe_options.values()):
        parser.error("Probe limits must be finite and positive.")
    import_options = {}
    if args.extensions is not None:
        import_options["extensions"] = (
            None if args.extensions == "*" else args.extensions.split(",")
        )
    try:
        from align import (
            align_metadata,
            load_schema,
            read_json,
            validate_annotations,
            validate_mapping,
        )
        from export import export_metadata
        from extract import extract_metadata, inspect_ffprobe

        import_files = importlib.import_module("import").import_files
        if os.path.lexists(args.output.expanduser()):
            raise FileExistsError("Choose a new output folder.")
        # One schema snapshot per run; unavailable validation must not discard evidence.
        schema_context = load_schema(args.schema)
        metadata = read_json(args.metadata) if args.metadata else {}
        validate_annotations(metadata, bestmeta=args.bestmeta)
        mapping = read_json(args.mapping) if args.mapping else None
        if mapping is not None:
            validate_mapping(mapping)
        probe_info = inspect_ffprobe(args.ffprobe)
        tool = probe_info["executable"]
        run_info = {
            "software": {"name": "PyVMM", "version": __version__},
            "python": sys.version,
            "ffprobe": probe_info,
        }
        assets, issues = import_files(args.inputs, exclude_paths=[args.output], **import_options)
        records = (
            align_metadata(
                extract_metadata(asset, ffprobe=tool, **probe_options),
                metadata,
                schema_context=schema_context,
                mapping=mapping,
                bestmeta=args.bestmeta,
            )
            for asset in assets
        )
        summary = export_metadata(
            records,
            args.output,
            import_issues=issues,
            schema_info=schema_context["provenance"],
            run_info=run_info,
        )
        print(json.dumps(summary))
        return 0 if summary["success"] else 1
    except (
        KeyboardInterrupt,
        ImportError,
        OSError,
        ValueError,
        RuntimeError,
    ) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        print("Interrupted." if interrupted else f"Error: {error}", file=sys.stderr)
        if isinstance(error, ImportError):
            print("Install the dependencies listed in README.md.", file=sys.stderr)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr)
        return 130 if interrupted else 2


if __name__ == "__main__":
    raise SystemExit(main())

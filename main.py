"""Run the pipeline: import -> extract -> align -> export."""

__version__ = "0.2.0.dev0"

import argparse
import importlib
import json
import sys
from pathlib import Path

import align
import export
import extract

import_files = importlib.import_module("import").import_files  # "import" is a keyword


def _positive(kind):
    def parse(text):
        value = kind(text)
        if not 0 < value < float("inf"):
            raise argparse.ArgumentTypeError("must be a finite number above zero")
        return value

    return parse


def _parser():
    parser = argparse.ArgumentParser(
        description="Extract video metadata with FFprobe and align it to BeStMeta.",
        epilog="Exit status: 0 all records valid, 1 finished with drafts or errors, "
        "2 could not run, 130 interrupted.",
    )
    parser.add_argument("--version", action="version", version=f"PyVMM {__version__}")
    parser.add_argument("inputs", nargs="+", type=Path, help="video files or folders")
    parser.add_argument("-o", "--output", type=Path, required=True, help="new output folder")
    parser.add_argument("--metadata", type=Path, help="researcher metadata (JSON)")
    parser.add_argument("--schema", type=Path, help="JSON Schema (default: bundled BeStMeta)")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable (default: PATH)")
    parser.add_argument("--timeout", type=_positive(float), default=60, help="seconds per file")
    parser.add_argument(
        "--max-output-bytes", type=_positive(int), default=extract.MAX_OUTPUT_BYTES,
        help="FFprobe output cap per stream and file",
    )
    parser.add_argument(
        "--extensions", type=lambda text: [e for e in text.split(",") if e.strip()],
        help="folder search filter, e.g. .mp4,.avi ('*' = every file)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list files as they are processed")
    return parser


def main(argv=None):
    """Run the pipeline and return the exit status."""
    args = _parser().parse_args(argv)
    issues = []
    try:
        probe = extract.inspect_ffprobe(args.ffprobe)
        metadata = align.load_metadata(args.metadata)
        schema = align.load_schema(args.schema)
        for issue in schema["issues"]:
            print(f"{issue['severity']}: {issue['code']}: {issue['message']}", file=sys.stderr)
        options = {} if args.extensions is None else {"extensions": args.extensions}

        def records():
            for number, asset in enumerate(import_files(args.inputs, issues, **options), 1):
                if args.verbose:
                    print(f"[{number}] {asset['relative_path']}", file=sys.stderr)
                extraction = extract.extract_metadata(
                    asset,
                    ffprobe=probe["executable"],
                    timeout=args.timeout,
                    max_output_bytes=args.max_output_bytes,
                )
                yield align.align_metadata(extraction, metadata, schema)

        run = {"software": {"name": "PyVMM", "version": __version__}, "python": sys.version, "ffprobe": probe}
        summary = export.export_metadata(
            records(), args.output, run=run, schema=schema["info"], import_issues=issues
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary))
    if not summary["success"]:
        print(
            f"{summary['draft']} of {summary['files']} records are drafts, {summary['errors']} errors. "
            "See 'missing_required' above and validation/issues in metadata.json.",
            file=sys.stderr,
        )
    return 0 if summary["success"] else 1


if __name__ == "__main__":  # pragma: no cover - run by a subprocess test
    raise SystemExit(main())

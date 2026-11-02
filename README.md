# PyVMM

Extract local scientific-video metadata, align known fields with
[BeStMeta](https://github.com/Helmholtz-UFZ/BeStMeta), and retain unmapped metadata.

| File | Responsibility |
| --- | --- |
| `main.py` | Parse options and run the four stages. |
| `import.py` | Discover files, identify duplicates and calculate SHA-256 fingerprints. |
| `extract.py` | Run FFprobe with time/output limits; retain raw metadata and derive technical fields. |
| `align.py` | Merge researcher metadata, report conflicts and optionally validate a local schema. |
| `export.py` | Stream JSON/CSV into a new output directory; publish both together. |

## Setup

Requires Python 3.11+ and native [FFprobe](https://ffmpeg.org/download.html).
Keep the environment, data, schemas and results outside this directory. PowerShell:

```powershell
py -3 -m venv "$env:LOCALAPPDATA\pyvmm-venv"
$python = "$env:LOCALAPPDATA\pyvmm-venv\Scripts\python.exe"
& $python -m pip install jsonschema==4.26.0 referencing==0.37.0 rfc3339-validator==0.1.4 rfc3986-validator==0.1.1
$ffprobe = 'C:\tools\ffmpeg\bin\ffprobe.exe' # Set your installed executable path.
& $ffprobe -version
```

Run from the directory containing the six project files. On Linux,
use `python3 -m venv`, the environment's `bin/python`, and `ffprobe` on PATH.
Linux outputs require atomic no-replace directory rename support. In WSL, use a
Linux output directory, or run Windows Python for output on a Windows drive.

## Run

```powershell
& $python -B main.py 'D:\videos' -o 'D:\results\run-001' --ffprobe $ffprobe
```

Supply files or folders. Folders are searched recursively for common video
extensions; explicit files are probed regardless of extension. Links and junctions
are skipped. Repeated paths are processed once; duplicate content is retained and
reported. Unreadable or corrupt inputs produce diagnostics while other files continue.
Override folder filtering with `--extensions .mp4,.avi,.cine`, or use
`--extensions '*'` to inspect every file. Explicit input files bypass this filter.

Add researcher context and a local schema when available:

```powershell
& $python -B main.py 'D:\videos\recording.mp4' -o 'D:\results\run-002' --ffprobe $ffprobe --metadata 'D:\study.json' --schema 'D:\schemas\bestmeta.json'
```

`--metadata` accepts a UTF-8 JSON object shared by all input files. Use BeStMeta
field names directly, or this envelope for additional research metadata:

```json
{
  "bestmeta": {"dataset_id": "https://example.org/studies/my-study"},
  "annotations": {"specimen": "sample-01", "capture_rate_hz": 1000}
}
```

Playback timing is kept separate from acquisition timing; frame counts are never
estimated. Missing context remains missing. Measured fields take precedence over
conflicting annotations; JSON retains both values and their provenance. An explicit
`dataset_id` replaces the generated file identifier.

Schemas are optional local files, captured once with their hash and full document.
`jsonschema` selects the declared draft, or its installed default when `$schema`
is absent; the active dialect is recorded in the export.
References within the document work; network retrieval is disabled. Missing or
unsupported schemas and failed validation produce drafts without discarding metadata.
Unknown fields survive even when the schema rejects them. Changed field meanings
require reviewing the mappings in `align.py`. Validation checks declared structure
and rules, not biological accuracy. Installed standard format checks are enabled;
unknown/custom formats remain annotations. Patterns use Python regex semantics.
Codec and container candidates are validated by that schema, without duplicated
value lists or automatic replacement of unknown values with `other`.

## Results and validation

Each new output directory contains:

- `metadata.json`: complete records, raw FFprobe sections, aligned metadata,
  additional annotations, provenance, conflicts, diagnostics, schema and run identity.
- `metadata.csv`: one row per file; `bestmeta.*`, `technical.*`, `raw.*` and
  `annotations.*` columns. Arrays occupy JSON cells. Literal `~` and `.` in key
  components become `~0` and `~1`. Formula-like text is prefixed with an apostrophe
  for spreadsheet safety. JSON preserves the original values.

Existing output paths are refused. Both outputs are staged and published together
after writing succeeds; failed exports clean up their temporary files when possible.

Exit codes: **0** = every record validated with no errors; **1** = results exported
with drafts, unvalidated records or input errors; **2** = configuration/export
failure; **130** = interrupted. A run without a schema normally returns **1**.

To validate with your test videos, run a small representative folder first, then
inspect the exported evidence:

```powershell
$report = Get-Content 'D:\results\run-001\metadata.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$report.summary
$report.import_issues
$report.records | ForEach-Object {
    $_.extraction.asset.relative_path
    $_.validation
    $_.issues
}
Get-FileHash 'D:\videos\recording.mp4' -Algorithm SHA256
```

Compare the hash with the corresponding record's `extraction.asset.sha256`;
compare `technical` with a direct FFprobe inspection and independently known
dimensions, reported counts and playback timing. Check unknown fields in JSON
and CSV, and review every conflict and validation error. Repeat with no schema,
your current schema and an updated schema: evidence must remain present in all
three outputs. Include a corrupt file and an unavailable input to confirm that
good recordings still export. Reusing an output path must fail without changing it.

FFprobe reads metadata; it does not certify that every frame decodes. When needed,
check the video separately using FFmpeg:

```powershell
& 'C:\tools\ffmpeg\bin\ffmpeg.exe' -v error -xerror -i 'D:\videos\recording.mp4' -map '0:v:0' -f null -
```

Probing defaults to 60 seconds and 16 MiB combined stdout/stderr per file;
override with `--timeout 120 --max-output-bytes 33554432`.
Allowed self-contained demuxers are listed in `extract.py`.
Inputs must be local, self-contained media. Unsupported inputs retain their file
identity and failure diagnostics.
Probe JSON is limited to 64 nesting levels. Rejected JSON bytes remain in
`extraction.tool.unparsed_stdout_base64`; the remaining files still export.

## Adapt the pipeline

Stages exchange JSON-compatible dictionaries: `import_files` returns assets and
issues; `extract_metadata` returns `asset`, `raw`, `technical`, `tool` and `issues`;
`align_metadata` returns a record; `export_metadata` accepts an iterable of records.
Keep these contracts when replacing a stage. Extraction owns backend interpretation;
alignment owns field meanings; export owns serialization.

The seven standard technical-field projections are defined once in `align.FIELD_MAP`.
Override them per call using tuples of destination keys, for example:

```python
from align import FIELD_MAP, align_metadata

mapping = {**FIELD_MAP, "height": ("image", "height_px")}
record = align_metadata(extraction, field_map=mapping)
```

An empty map disables these projections. Raw and technical metadata remain intact.
Overlapping paths are rejected; literal dots and tildes in keys are supported.
Acquisition timing still requires independently supplied context.
After updating Python, FFprobe, dependencies, schemas or mappings, rerun the
validation checks above.

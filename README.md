# PyVMM

Extract metadata from local scientific videos, align known fields with
[BeStMeta](https://github.com/Helmholtz-UFZ/BeStMeta), and retain everything else.
The project consists of five Python files and this README.

| File | Responsibility |
| --- | --- |
| `main.py` | Parse options and run the four stages. |
| `import.py` | Discover files, identify duplicates and calculate SHA-256 fingerprints. |
| `extract.py` | Run FFprobe with time/output limits; retain raw metadata and derive technical fields. |
| `align.py` | Merge researcher metadata, report conflicts and optionally validate a local schema. |
| `export.py` | Stream JSON/CSV into a new output directory; publish both together. |

## Setup

Requires Python 3.11+ and a native [FFprobe](https://ffmpeg.org/download.html)
executable. Keep the environment, data, schemas and results outside this code
directory. In PowerShell:

```powershell
py -3 -m venv "$env:LOCALAPPDATA\pyvmm-venv"
$python = "$env:LOCALAPPDATA\pyvmm-venv\Scripts\python.exe"
& $python -m pip install jsonschema==4.26.0 referencing==0.37.0 rfc3339-validator==0.1.4 rfc3986-validator==0.1.1
$ffprobe = 'C:\tools\ffmpeg\bin\ffprobe.exe' # Set your installed executable path.
& $ffprobe -version
```

Run commands from the directory containing the six project files. On Linux,
use `python3 -m venv`, the environment's `bin/python`, and `ffprobe` on PATH.
No package installation of PyVMM is needed.
Linux outputs require atomic no-replace directory rename support. In WSL, use a
Linux output directory, or run Windows Python for output on a Windows drive.

## Run

```powershell
& $python -B main.py 'D:\videos' -o 'D:\results\run-001' --ffprobe $ffprobe
```

Supply one or more files or folders. Folders are searched recursively for common
video extensions; explicitly supplied files are probed regardless of extension.
Symbolic links and junctions are skipped. Repeated paths are processed once;
identical content at different paths is retained and reported. Individual unreadable
or corrupt files produce diagnostics while other files continue.

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

Missing biological/acquisition context remains missing. File playback rate and
duration never become camera acquisition rate or recording duration. Frame counts
come only from reported counts. Measured fields take precedence over conflicting
annotations; both values and their provenance remain in JSON. An explicit
`dataset_id` replaces the generated content-based file identifier.

The schema is optional and never downloaded automatically. A selected schema is
captured once with its hash and full document. Supported JSON Schema drafts are
handled by `jsonschema`; a schema without `$schema` uses Draft 2019-09. Local
references work within that document; remote retrieval is disabled. Unsupported
required vocabularies, missing schemas and validation failures produce drafts
with diagnostics. New schema fields and metadata outside the schema are retained
even when validation rejects them. Changed field meanings require reviewing the
explicit mappings in `align.py`. Validation checks structure and declared rules;
it cannot establish biological accuracy or completeness.
Installed standard format checks are enabled, with strict date, date-time and URI
checks. Unknown/custom formats remain annotations, as allowed by JSON Schema;
patterns use Python regular-expression semantics.

## Results and validation

Each new output directory contains:

- `metadata.json`: complete records, raw FFprobe sections, aligned metadata,
  additional annotations, provenance, conflicts, diagnostics, schema and run identity.
- `metadata.csv`: one row per file; `bestmeta.*`, `technical.*`, `raw.*` and
  `annotations.*` columns. Arrays occupy JSON cells. Literal `~` and `.` in key
  components become `~0` and `~1`. Formula-like text is prefixed with an apostrophe
  for spreadsheet safety. JSON preserves the original values.

Existing output paths are refused. Outputs are staged beside the target and
published together after writing succeeds. A failed export removes its staging
directory when possible. Memory holds one extracted record at a time; CSV rows
are spooled to temporary disk, and discovered file identities are held in memory.

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

Probing defaults to 60 seconds and 16 MiB combined stdout/stderr per file.
These limits are parameters of `extract_metadata`. Local self-contained media
demuxers are allowed explicitly in `extract.py`; playlists, external sequences
and network inputs are outside that scope. Unsupported media retains its file
identity and failure diagnostics. Edit the relevant stage to change discovery,
extraction, mappings or export behavior; no plugin framework is involved.

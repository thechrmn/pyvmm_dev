# PyVMM

Extract scientific-video metadata, optionally map it to a metadata standard and
validate it against a local **JSON Schema**. Raw evidence is always retained.
The project is five Python files and this README.

| File | Responsibility |
| --- | --- |
| `main.py` | Run import, extraction, alignment and export. |
| `import.py` | Discover local files and calculate SHA-256 fingerprints. |
| `extract.py` | Run bounded FFprobe; retain raw metadata and derive technical facts. |
| `align.py` | Map fields, merge context, retain conflicts and optionally validate. |
| `export.py` | Stream JSON/CSV and publish both together into a new directory. |

## Setup and schema-free use

Requires Python 3.11+ and native [FFprobe](https://ffmpeg.org/download.html).
**No third-party Python packages are required without schema validation.**
Keep environments, data, optional configuration and outputs outside this directory.
In PowerShell:

```powershell
py -3 -m venv "$env:LOCALAPPDATA\pyvmm-venv"
$python = "$env:LOCALAPPDATA\pyvmm-venv\Scripts\python.exe"
$ffprobe = 'C:\tools\ffmpeg\bin\ffprobe.exe' # Set your installed executable path.
& $python -B main.py 'D:\videos' -o 'D:\results\run-001' --ffprobe $ffprobe
```

Run from the directory containing these six files. On Linux, use `python3 -m venv`,
the environment's `bin/python`, and FFprobe on PATH. WSL output must use a Linux
filesystem supporting atomic no-replace directory rename; use Windows Python
for output on a Windows drive.

By default, `metadata` contains technical fields with their original names, merged
with an optional `--metadata 'D:\study.json'` object shared by all input files.
Generic metadata is taken literally, including keys named `metadata`, `bestmeta`
or `annotations`. Measured values take precedence on conflict; both values and
their provenance remain in JSON. Playback timing never becomes acquisition timing,
and missing frame counts are never estimated.

## Optional schemas and mappings

Install these packages only to enable schema validation:

```powershell
& $python -m pip install jsonschema==4.26.0 referencing==0.37.0 rfc3339-validator==0.1.4 rfc3986-validator==0.1.1
```

The RFC packages are used only when checking date-time or URI strings.

`--schema` validates the candidate `metadata` object. A schema does not determine
field meanings or rename measurements automatically. Use `--mapping` when target
names or structure differ. For example, an external UTF-8 `mapping.json`:

```json
{
  "/technical/width": "/image/columns",
  "/technical/height": "/image/rows",
  "/asset/sha256": "/identifier"
}
```

```powershell
& $python -B main.py 'D:\videos' -o 'D:\results\run-002' --ffprobe $ffprobe --schema 'D:\schemas\custom.json' --mapping 'D:\mapping.json' --metadata 'D:\study.json'
```

Mappings replace the default technical-field mapping and work without a schema
too. Sources are JSON Pointers into `extraction`; targets create object fields in
`metadata`. Escape `/` as `~1` and `~` as `~0`. Sources may index arrays; map an
entire array to a target field when needed. Overlapping targets are rejected;
unavailable source values produce diagnostics. An empty mapping `{}` uses only
researcher metadata. Unmapped raw and technical fields remain in the exports.

For [BeStMeta](https://github.com/Helmholtz-UFZ/BeStMeta), opt into its adapter:

```powershell
& $python -B main.py 'D:\videos' -o 'D:\results\run-003' --ffprobe $ffprobe --bestmeta --schema 'D:\schemas\bestmeta.json' --metadata 'D:\study.json'
```

`--bestmeta` and `--mapping` are alternatives. The BeStMeta adapter also works
without a schema. It accepts bare BeStMeta metadata or the legacy envelope
`{"bestmeta": {...}, "annotations": {...}}`. Its generated file identifier may
be replaced by a supplied `dataset_id`. Output always uses the neutral `metadata`
key and `metadata.*` CSV columns, including in BeStMeta mode.

Schemas are captured once with their hash and complete document. The declared
draft is used, or the installed validator's default if none is declared; the
active dialect is recorded. References within the document work; network retrieval
is disabled. Missing files, unsupported schemas or missing validation packages
produce unvalidated drafts while preserving evidence. Unknown metadata survives
even when rejected by the schema. Standard format checks are enabled; unknown
formats remain annotations. Patterns use Python regex semantics. Validation checks
declared rules, not scientific accuracy.

## Outputs and validation

- `metadata.json`: complete evidence, mapped metadata, context, provenance,
  conflicts, diagnostics, schema snapshot and tool identity.
- `metadata.csv`: one row per file; `metadata.*`, `technical.*`, `raw.*` and
  supplemental `annotations.*` columns. Arrays occupy JSON cells. Literal `~`
  and `.` in key components become `~0` and `~1`. Formula-like text is prefixed
  with an apostrophe. JSON preserves original values.

`validation.ok` reports whether the record completed without errors and passed
any requested validation. `schema_valid` reports schema conformance; `valid` also
accounts for operational errors and conflicts. Both are `null` when validation
is not requested or unavailable. Intentional schema-free runs have
`schema_status: "not_requested"` and can succeed without claiming conformance.
The summary counts validated, unvalidated and draft records separately.

Exit codes: **0** = all records succeeded; **1** = exported drafts or input errors;
**2** = configuration/export failure; **130** = interrupted. Existing output paths
are refused. Both outputs are staged and published together after writing succeeds.

To validate with your videos:

```powershell
$report = Get-Content 'D:\results\run-001\metadata.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$report.summary
$report.import_issues
$report.records | ForEach-Object { $_.validation; $_.issues; $_.conflicts }
Get-FileHash 'D:\videos\recording.mp4' -Algorithm SHA256
```

Compare hashes with `extraction.asset.sha256`, and compare technical facts with
direct FFprobe inspection and independently known measurements. Repeat without a
schema, with BeStMeta, and with a different schema/mapping: raw evidence and custom
context must remain present. Include corrupt and missing inputs; good files must
still export. Reusing an output directory must fail without changing it.
FFprobe checks metadata; full decoding is a separate optional check:

```powershell
& 'C:\tools\ffmpeg\bin\ffmpeg.exe' -v error -xerror -i 'D:\videos\recording.mp4' -map '0:v:0' -f null -
```

Folders are searched recursively for common video extensions; explicit files
bypass that filter. Override it with `--extensions .mp4,.avi` or
`--extensions '*'` for all files. Links/junctions are skipped; duplicate content
at distinct paths is retained and reported. Probing defaults to 60 seconds and
16 MiB combined stdout/stderr; override with `--timeout 120 --max-output-bytes 33554432`.
Allowed self-contained demuxers are listed in `extract.py`. Inputs must be local,
self-contained media. Probe JSON is limited to 64 nesting levels; rejected bytes
remain in `extraction.tool.unparsed_stdout_base64` while other files continue.

# PyVMM: scientific video metadata, aligned to BeStMeta

Reads the metadata stored inside video files, aligns it with the
[BeStMeta](https://github.com/Helmholtz-UFZ/BeStMeta) schema (or any JSON Schema),
validates the result, and writes one JSON document and one spreadsheet. Nothing is
decoded and nothing is guessed: every value says where it came from, and what the
files cannot tell you stays visible as *missing*.

```
videos/  ->  import.py  ->  extract.py  ->  align.py  ->  export.py  ->  metadata.json + metadata.csv
             find, hash     FFprobe        map + validate  new folder
```

## Install and run

Needs Python 3.11 or newer and `ffprobe` (part of FFmpeg) on your `PATH`.

```
pip install -r requirements.txt          # only needed for validation
python main.py videos/ -o results --metadata my_dataset.json
```

Without `jsonschema` the pipeline still runs and exports drafts; it says so and does not validate.
`results` must not exist yet; it is never overwritten.

| Option | Meaning |
|---|---|
| `inputs` | video files and/or folders (folders are searched recursively) |
| `-o, --output` | new output folder (required) |
| `--metadata FILE` | what you know and the files do not (below) |
| `--schema FILE` | JSON Schema to validate against (default: bundled BeStMeta) |
| `--extensions .mp4,.avi` | folder search filter (`*` = every file); named files are always read |
| `--ffprobe PATH`, `--timeout S`, `--max-output-bytes N` | FFprobe location and per-file limits (60 s, 16 MiB) |
| `-v` | list files on stderr while they are processed |

Exit status:

| Status | Meaning |
|---|---|
| 0 | every file read and every record valid |
| 1 | finished, but some records are drafts or some files had errors; outputs are written |
| 2 | could not run (bad arguments, FFprobe missing, unreadable metadata, output exists) |
| 130 | interrupted; nothing is written |

## What you provide

BeStMeta describes a study, not just a file. Most of its required fields (title, licence,
hardware, recording software) cannot be read from a video. The run summary lists exactly
which ones are missing (`missing_required`, with counts). Put them in one JSON file:

<!-- example-metadata -->
```json
{
  "dataset_title": "Open-field tracking, cohort 3",
  "dataset_license": "CC-BY-4.0",
  "hardware_and_acquisition": {
    "hardware": {
      "device_type": "in_house_system",
      "in_house_system_description": "Overhead USB camera above a 40 cm arena",
      "in_house_system_components": [
        "USB camera",
        "LED panel"
      ],
      "in_house_system_designer": null,
      "field_of_view_width": 400,
      "field_of_view_height": 400,
      "field_of_view_unit": "mm"
    },
    "acquisition": {
      "recording_software_name": "VendorRec",
      "recording_software_version": "2.1"
    }
  }
}
```

* The file is either BeStMeta fields directly (as above) or an envelope with the keys
  `bestmeta` and `annotations`; `annotations` is free-form context kept next to the record.
* The same file applies to **every** file of a run. Run once per group of recordings that
  share their acquisition setup.
* Duplicate keys, `NaN`, and non-finite numbers are rejected rather than guessed at.

## Rules that keep records honest

* **Measured wins.** Width, height, codec, container, bit depth, colour mode and frame count
  come from the file. If your metadata disagrees, the measured value stays and the
  disagreement is listed in `conflicts`; the record is not valid until you resolve it.
* **Timing is yours to override.** `frame_rate` and `recording_duration` are filled from the
  file's own timeline, but a time-lapse or high-speed recording is played back at a different
  rate than it was captured. A value in your metadata replaces them (the file's values stay in
  `extraction.technical`), and the schema still checks it.
* **`other` means "not in the schema's list".** `vp9`, Matroska/WebM, MPEG-TS, Ogg, ProRes, DV and
  others are written as `other` where the schema's enum offers it; the real name stays in
  `extraction.technical`. Your metadata may say `other` too.
* **No invented numbers.**
  * Frame counts are reported only when the container declares one (MP4, AVI, IVF do; Matroska,
    WebM, MPEG-TS, Ogg usually do not). Count them yourself if you need them, e.g.
    `ffprobe -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 FILE`
    (decodes the video), and add `total_frame_count`.
  * Raw elementary streams (`.h264`, `.hevc`, `.mjpeg`, `.m4v`, `.m2v`) store no timing; FFprobe
    reports a default of 25 fps for them, which is not a measurement. Frame rate and duration
    are therefore left out (`elementary_stream_timing_unknown`) and must come from you.
  * Bit depth comes from the stream, else from the pixel format's component depth.
    BeStMeta requires it for FFV1 and raw video.
  * Recording start time is not taken from the container's `creation_time` (often the time of
    muxing, not of recording); it stays in `extraction.raw`.
* **One identifier per file.** `dataset_id` is the SHA-256 of the bytes as an RFC 6920 URI
  (`ni:///sha-256;...`). Set your own `dataset_id` to group files into a study.

## Output

`metadata.json` (complete) and `metadata.csv` (one flattened row per file; formulas are
neutralised for spreadsheets). The folder appears only when the run is complete.

```
run            software, Python, FFprobe version and arguments
schema         path, sha256, dialect, status
records[]      per file:
  extraction.asset      path, relative_path, size_bytes, mtime_ns, sha256
  extraction.raw        FFprobe format/streams/chapters/programs, unchanged
  extraction.technical  normalised values, with *_source fields saying where they came from
  bestmeta              the aligned record
  annotations           your free-form context
  provenance            for every BeStMeta path: each candidate value, its source, method, and whether it was selected
  conflicts             measured vs supplied disagreements
  issues                everything noteworthy: severity, code, message, stage
  validation            valid, schema_valid, schema_status, errors, missing_required
import_issues  problems found while locating files
summary        files, valid, draft, errors, success, missing_required (counts)
```

Output is deterministic: the same inputs give the same bytes (no timestamps).
`error` issues mean a file's evidence is unusable; whether metadata fits the schema is reported in
`validation` and `draft`, so `summary.errors` counts unreadable files, not incomplete metadata.
`valid` means schema-valid, no conflicts, and no `error` issues.

| Issue | Severity | Meaning |
|---|---|---|
| `input_not_found`, `input_unsupported`, `folder_unreadable`, `file_unreadable` | error | an input could not be used |
| `no_inputs` | error | nothing readable was found |
| `symlink_skipped` | warning | a link inside a searched folder was not followed (links you name are followed) |
| `duplicate_content` | warning | identical bytes to another file |
| `ffprobe_failed`, `ffprobe_timeout`, `ffprobe_output_limit`, `ffprobe_unavailable`, `invalid_probe_json` | error | the file could not be read |
| `input_changed` | error | the file changed while it was processed; nothing from it is trusted |
| `video_stream_not_found` | error | no video stream (cover art does not count) |
| `ffprobe_diagnostic` | warning | text FFprobe printed on stderr |
| `multiple_video_streams` | warning | the default stream, else the first, was used |
| `nominal_frame_rate`, `possible_variable_frame_rate` | warning | rate source or constancy is uncertain |
| `implausible_average_frame_rate` | warning | the average exceeded the base rate (seen with DV); the base rate was used |
| `duration_mismatch`, `container_duration_fallback`, `video_duration_ambiguous` | warning | duration source notes; ambiguous means none is reported |
| `elementary_stream_timing_unknown` | warning | raw stream: no timing reported |
| `technical_value_unavailable` | warning | width, height, rate or duration could not be determined |
| `jsonschema_not_installed`, `schema_unavailable`, `schema_validation_unavailable` | warning | validation could not run; records are drafts |
| `metadata_conflict`, `bestmeta_validation_failed` | warning | your metadata disagrees with the file / the schema rejects the record |

## Safety

FFprobe runs with a time limit, an output cap, a file-only protocol whitelist and a demuxer
whitelist. The whitelist matters for correctness as well as security: a text file that merely
*names* another video (a concat or playlist script) would otherwise be described as that
video. Remote `$ref`s in schemas are never fetched. Links found inside searched folders are not
followed. File names are never interpreted by a shell.

## Limits

* Video containers FFmpeg can read. HDF5 and image sequences (also listed by BeStMeta) are not
  read; replace `extract.py` for them (the stages only exchange plain dictionaries).
* Metadata is read, not verified: files are not decoded.
* `import.py` is named as requested; `main.py` loads it with `importlib` because `import` is a
  Python keyword. No other module imports it.
* Developed and tested on Linux with FFmpeg 6.1 and Python 3.12. `.github/workflows/test.yml` runs the
  suite on Linux, macOS and Windows; macOS and Windows have not been exercised by the author.

## Adapting

* Other schema: `--schema FILE` plus a matching `FIELD_MAP` in `align.py` (technical key to a
  destination path). Only `date`, `date-time` and `uri` formats are asserted.
* More containers: add the demuxer name to `DEMUXERS` in `extract.py`.
* Other metadata sources: a stage is any function with the same dictionaries in and out.

## Development

```
python -m unittest                       # needs ffmpeg and ffprobe to build test videos
```

The suite generates its videos with ffmpeg, so no binary files are stored. See
`schemas/README.md` for the bundled schema and how to update it, and
`docs/LIBRARY_EVALUATION.md` for why FFprobe and `jsonschema` were chosen.

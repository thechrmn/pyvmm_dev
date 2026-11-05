# Library evaluation

Why PyVMM extracts with **FFprobe** and validates with **`jsonschema`**. Measured on
2026-11-04 (Ubuntu 24.04, Python 3.12, FFmpeg/FFprobe 6.1.1, ExifTool 12.76, one CPU core).

## Method

* **Corpus.** 27 readable videos generated with ffmpeg's `lavfi` source, so frame counts,
  rates, sizes, codecs and pixel formats are known *exactly* from the generation parameters, plus
  7 unreadable or hostile files (truncated, empty, random bytes, a text file named `.mp4`, concat and
  HLS playlists that name other files). Containers: MP4, MOV, AVI, MKV, WebM, MPEG-TS, Ogg. Codecs:
  H.264, H.265, AV1, VP9, ProRes, MJPEG, MPEG-4, MPEG-2, Theora, FFV1, raw. Includes 1000 fps,
  NTSC 30000/1001, variable frame rate in MP4 and Matroska, rotation metadata, Unicode names.
* **Scoring.** Each library is run in its own virtual environment, one process per file (so crashes and
  hangs are visible), then once over all readable files for steady-state speed. Footprint is the
  installed size minus an empty virtual environment (16 MB).
* **Not covered.** The published datasets in `published_video_files.ods` live on Zenodo, Figshare,
  BioStudies and Google Drive, which the test environment could not reach, and no vendor-camera files
  were available. Synthetic media cannot reproduce every vendor quirk; DV and raw elementary streams
  were added *after* this table was made, from what the pipeline's own tests exposed (second table).

## Metadata extraction

| Backend | Python footprint | Opened | Size right | Rate right (of 24) | Frame counts given / **wrong** | Codec = FFmpeg name | Pixel format | Container | Bad files rejected | ms/file |
|---|---|---|---|---|---|---|---|---|---|---|
| **FFprobe** (CLI, JSON) | 0 MB | 27/27 | 27 | 24 | 21 / 0 | 27 | 27 | 27 | 7/7 | 52 (incl. process start) |
| MediaInfo (`pymediainfo`) | 14 MB | 27/27 | 27 | 24 | 25 / 0 | 8 | 0 | 27 | 7/7 | 1.8 |
| PyAV (`av`) | 127 MB | 27/27 | 27 | 24 | 21 / 0 | 27 | 27 | 27 | 7/7 | 1.1 |
| OpenCV (`opencv-python-headless`) | 221 MB | 27/27 | 26 | 24 | 27 / **1** | 17 | 0 | 0 | 7/7 | 1.0 |
| hachoir | 6 MB | 26/27 | 21 | 5 | 0 / 0 | 0 | 0 | 26 | 6/7 | 1.1 |
| imageio-ffmpeg | 165 MB | 27/27 | 27 | 22 | 0 / 0 | 25 | 27 | 0 | 7/7 | 47 |
| ExifTool (CLI) | 0 MB + Perl | 27/27 | 27 | 24 | 6 / 0 | 1 | 0 | 27 | 7/7 | 105 |

(Rate is scored with the fallback to `r_frame_rate` that FFprobe and PyAV need for Ogg/Theora.)

**FFprobe** gives exact codec, pixel-format and container names (the vocabulary BeStMeta's codec enum
already follows), exact rationals, no Python dependency, and states a frame count only when the file
does. It costs a process start per file (about 50 ms), small next to hashing the file.
**MediaInfo** is the strongest alternative: fast, rich, handles DV correctly. Its codec and container
names need an alias table, it reports no pixel format, and it returns frame counts without saying
whether they are stored or derived. **PyAV** gives the same data as FFprobe for 127 MB of bundled
FFmpeg and a decoder-dependent `codec_context.name` (`libdav1d` instead of `av1`). **OpenCV**
estimates the frame count as duration x rate and presents it as fact: 148 frames for a variable-rate
file that has 90. It also applies rotation metadata silently (240x320 for a 320x240 file). **hachoir**
misses most fields. **imageio-ffmpeg** parses FFmpeg's human-readable banner, which prints 1000 fps
as "1k fps" and so reports 0. **ExifTool** is excellent for tags but weak on stream properties.

### Added later: DV and raw elementary streams (25 fps DV; 30 fps raw streams without timing)

| Backend | DV | Raw H.264 / HEVC / MJPEG at 30 fps |
|---|---|---|
| FFprobe | **60000 fps** (avg rate is the time-base reciprocal; the base rate says 25) | 25 fps default for all; no duration |
| MediaInfo | 25 fps, 5 frames: correct | 31.579 fps / 30 fps, **16 frames** (true: 60), duration 1.9 s / 0.53 s; MJPEG: "no video track" |
| PyAV | 60000 fps | 25 fps default |
| OpenCV | 60000 fps, 12000 frames | 25 fps default |

No backend is trustworthy for raw streams, which carry no timing. FFprobe's mistakes are at least
*detectable* (average rate above the base rate; a known list of raw demuxers), while MediaInfo's and
OpenCV's are silent. PyVMM therefore applies two rules whatever the backend: an average rate above
the base rate is rejected, and raw elementary streams report no frame rate or duration.

## Schema validation (BeStMeta is JSON Schema 2019-09)

22 mutation cases of a schema-valid record (missing required field, enum, pattern, nested
`additionalProperties`, nullable handling, `bool` as integer, float integers, `if/then`, date-time
and URI formats, Unicode), each with a known verdict.

| Library | Dependencies / size | Wrong verdicts | us per validation | Notes |
|---|---|---|---|---|
| **`jsonschema`** + own format checks | 6 packages, 3 MB | **0** | 480 | selects the dialect from `$schema` |
| `jsonschema` alone | 6 packages, 3 MB | 2 | 420 | `date-time` and `uri` silently **not asserted** without extra packages |
| `jsonschema-rs` | 1 wheel, 14 MB | 0 | 3 | fastest; speed is irrelevant next to a 50 ms FFprobe call |
| `fastjsonschema` | 1 package, 1 MB | 0 | 36 | supports drafts 4-7 only. It **accepts** an instance violating `unevaluatedProperties` and `dependentRequired`, because unknown keywords are ignored; unsafe for a schema that may evolve |
| `jschon` | 2 packages, 1 MB | 3 | 2300 | formats not asserted by default |
| LinkML validator | 90 packages, 200 MB | 1 | 269000 | regenerates a closed schema, so it rejects top-level extras the published JSON Schema allows |

The default format check of `jsonschema` depends on which optional packages happen to be installed,
so results would differ between machines. PyVMM registers exactly three strict checks (`date`,
`date-time`, `uri`) itself, which also removes two unmaintained dependencies
(`rfc3339-validator`, `rfc3986-validator`). `jsonschema` is imported only when validating, so the
pipeline runs without it and reports records as drafts.

## Consequences in the code

* FFprobe with `-protocol_whitelist file` and a demuxer whitelist: a text file naming another video
  (concat script) made FFprobe describe *that other file*, which would have been filed under the wrong
  SHA-256.
* Frame counts only when stored; raw streams untimed; implausible average rates rejected.
* Bit depth falls back to FFprobe's own pixel-format table when the stream declares none (needed
  for FFV1 and raw video, where BeStMeta requires it).
* Remote `$ref` retrieval disabled (`jsonschema` otherwise fetches over HTTP with a warning).
* A future change of backend only has to reproduce `extraction.technical`; the test suite checks
  that contract against media of known properties.

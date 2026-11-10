# Assessment record

How the pipeline was assessed against criteria R1-R12 and refined until the instruments stopped finding
defects. Measured on Linux, Python 3.12 (3.11, 3.13 and 3.14 re-run), FFmpeg 6.1.1, one CPU core.

## Method

Every round runs the system against an independent oracle (media generated with known properties, never
FFprobe's own output), reads the code against the criteria, then measures. A finding counts as closed only
when a test exists that fails without the fix. "Converged" means a complete pass of all instruments after the
last source change found nothing new.

## Rounds

| Round | Instrument | Found | Closed by |
|---|---|---|---|
| 0 | The draft, executed on a synthetic corpus of 31-34 files | BeStMeta not the default, so a bare run validated nothing. Container/codec names outside the schema enums written verbatim; measured values beat researcher input, so even `other` became a conflict and MKV/WebM/TS/Ogg files could never validate. `frame_rate` and `recording_duration` (required) never filled, and one shared metadata file cannot carry per-file durations, so no mixed batch validated. Inputs behind a symlinked parent rejected. Linux-only `renameat2` (by inspection). Spurious "is not of type null" errors. 103 KB schema copied into every output; duplicated issue lists; CRLF in one file | rewrite; `tests/test_pipeline.py::BatchValidity`, `Behaviour`; `test_align.py::Mapping`, `Merging`, `Validation` |
| 1 | Library comparison (7 extractors, 5 validators; `LIBRARY_EVALUATION.md`) | OpenCV invents frame counts; `fastjsonschema` silently ignores 2019-09 keywords; `jsonschema` silently skips `date-time`/`uri` without extras; ffprobe `-format_whitelist` is a correctness control (a text file naming another video was described as that video) | FFprobe + own strict format checks + lazy `jsonschema`; `Safety`, `OracleTests` |
| 2 | More media (DV, raw streams, IVF, Y4M) and a reproducibility check | FFprobe reports 60000 fps for DV; raw `.h264`/`.mjpeg` get a default 25 fps that is not a measurement; FFprobe stderr embeds memory addresses, so output was not reproducible; `errors` counted incomplete metadata as unreadable files; `bit_depth`, required by BeStMeta for FFV1/raw, could never be filled | plausibility rule, untimed raw streams, address stripping, severity rule, pixel-format depth table |
| 3 | Coverage, lint, complexity | One test passed vacuously (four failure cases wrote the same file); dead branches and a redundant guard; worst function complexity F (58) | tests fixed, code deleted or split; no function above B |
| 4 | Mutation testing, 610 AST mutants | 44 survivors among the 506 judged in the first pass. Real gaps: issue order, Windows junctions, fsync, output layout, defaults, boundaries. **A real bug:** closing a pipe while a reader thread was blocked defeated the deadline whenever a wrapper's child held the pipes. One flaky test (random temp-dir names) | tests added, `_Reader`/`_reap` extracted; final 599 of 610 killed (98.2%) |
| 5 | Python 3.11-3.14, `jsonschema` 4.18-4.26 and absent, performance, 400-file fuzz with a new seed, 400-case random-metadata property test | Nothing in the product. One test invariant was too strict (provenance of an object that replaces a whole value) | test refined |

| 6 | First GitHub Actions run (screenshot, then the log) | All 16 jobs red. The log of the ubuntu job: `setup-ffmpeg` installed the static **FFmpeg 7.0.2**, whose `ffprobe` **segfaulted (status -11) on the MPEG-TS fixture**; the pipeline reported it correctly and carried on, but the two oracle tests failed. Not reproducible with conda 7.0.2 (even with heap scribbling), 6.1.1, 9.0.2 or a static master build, and I could not obtain the static 7.0.2 binary, so **the exact trigger is unknown**. Also found while investigating: tests crashed instead of skipping when an encoder is missing (this build has no `libsvtav1`); one test did not test what it claimed (never passed a Latin-1 byte); Windows-hostile filenames and text reads | crash fallback (retry with only `format` and `streams`, recorded as `ffprobe_optional_sections_skipped`); oracle tests skip, not fail, when FFprobe itself crashes; fixture skipping; a CI step that probes a generated TS file with each section set; Latin-1 test fixed. Simulated with fake ffprobes: crash on chapters/programs, full suite passes; crash on every `.ts`, 3 skips |

The 11 surviving mutants are arbitrary constants (default timeouts, a cache size, buffer sizes), equivalent
mutations (a regex-group default, `read(MAX+1)` versus `+2`) or unobservable behaviour (`daemon=True`, a
`break` after the process is already killed). `extract.py` was re-run after the crash fallback; the other files
are unchanged since their runs. Validating the tool with a null mutant (it must survive) caught a spurious
100% caused by `ast.unparse` changing quote style under a regex-based documentation test.

## Criteria

| | Evidence |
|---|---|
| R1 Correctness | Every video's width, height, rate, duration, codec, container, bit depth and colour mode is checked against how it was generated; unreadable and hostile files must yield no data |
| R2 Completeness | Failure modes enumerated as documented issue codes (a test ties README to source); boundaries tested (size limits, rate tolerance, leap seconds, empty input) |
| R3 Simplicity | 1,161 to 839 source lines while adding capability; no mandatory Python dependency; custom ctypes, pipe polling and schema-resource walking removed |
| R4 Clarity | Single-purpose functions; stages exchange plain dictionaries; docstrings state contracts |
| R5 Modularity | Only `main.py` loads `import.py`; no stage imports another |
| R6 Consistency | One line-ending convention, pathlib throughout, one issue format, ruff clean (broad rule set) |
| R7 Robustness | 60 seeded plus 400 extra corrupted files; timeouts, output caps, cleanup on failure and interrupt, change-during-read detection |
| R8 Testability | 255 tests, 100% line and branch coverage, deterministic, videos generated at test time |
| R9 Maintainability | No function above complexity B (average 4.9); schema update procedure with a pinned hash |
| R10 Efficiency | 1 GB/s hashing, about 40 ms per file, flat 50 MB memory (README, Performance) |
| R11 Security | No shell, protocol and demuxer whitelists, no remote `$ref`, links in searched folders not followed, spreadsheet formulas neutralised, input size caps |
| R12 Documentation | README example is the tested example; issue codes tied to source; decisions in `LIBRARY_EVALUATION.md` and here |

## Not verified

* **GitHub Actions.** The ubuntu failure is understood (an FFprobe segfault) and handled, but I have only the
  ubuntu log, not the macOS and Windows ones, and nothing has run on the fixed version there yet. Windows and
  macOS code paths (junctions, batch-file refusal) are unit-tested by simulation only.
* **Real recordings.** The published datasets in `published_video_files.ods` and vendor-camera files were
  unreachable. Synthetic media cannot reproduce every vendor quirk; DV and raw streams show that such
  quirks exist.
* **Other FFmpeg versions.** Only 6.1.1. FFprobe's JSON has been stable for years, but the weekly CI run is
  the guard.
* **Not supported:** HDF5 and image sequences, per-file researcher metadata (run once per group instead),
  frame counts for containers that store none.

## Decisions that are yours

1. `frame_rate` and `recording_duration` are filled from the file's own timeline but a researcher value
   replaces them (capture and playback rates differ for time-lapse and high-speed recordings). The draft
   never filled them.
2. A value the schema cannot name becomes `other`; the real name stays in `extraction.technical`.
3. Raw elementary streams report no timing.
4. Validation findings are warnings; `errors` counts unreadable files. Exit status 1 means drafts.
5. One metadata file per run.
6. The repository has no LICENSE (deleted in commit `d4651e7`); the proposal promises MIT or CC-BY. Restore
   one before release.

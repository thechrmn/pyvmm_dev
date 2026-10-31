"""Evidence-preserving alignment to the pinned BeStMeta JSON Schema.

Each video receives a file-centric VTADataset view. A SHA-256 identifier is a
fallback identifier for that view, not a claim that a video is an entire study.
Scientific context and the dataset's licence must be supplied by the researcher.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft201909Validator, FormatChecker, ValidationError, validators
from referencing import Registry
from rfc3339_validator import validate_rfc3339
from rfc3986_validator import validate_rfc3986

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_DEPTH = 64
SCHEMA_COMMIT = "6bf77e27fe274481c06ee9e4b52e2befdcdb36d7"
SCHEMA_SHA256 = "446af0cffa654d6d8bd482dc208b41a3f04cf5cd73cc9ea72a1cab77e9cfdca3"

# Explicit format implementations keep validation independent of unrelated
# optional packages installed in the caller's Python environment.
_FORMATS = FormatChecker(formats=[])
_REGISTRY = Registry()  # Immutable; the default retriever rejects external references.


@_FORMATS.checks("date")
def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return True  # JSON Schema's type constraint handles non-strings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, re.ASCII):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@_FORMATS.checks("date-time")
def _valid_datetime(value: Any) -> bool:
    return not isinstance(value, str) or bool(validate_rfc3339(value.upper()))


@_FORMATS.checks("uri")
def _valid_uri(value: Any) -> bool:
    return not isinstance(value, str) or bool(validate_rfc3986(value, rule="URI"))


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_json(path: str | Path) -> dict:
    source = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(source, flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1  # The stream owns the descriptor from this point.
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError(f"Metadata source must be a regular file: {path}")
            # Size is an allocation hint: the fallback still captures file growth.
            limit = min(observed.st_size, MAX_METADATA_BYTES) + 1
            content = stream.read(limit)
            if len(content) == limit:
                content += stream.read(MAX_METADATA_BYTES + 1 - limit)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(content) > MAX_METADATA_BYTES:
        raise ValueError(f"Metadata file exceeds {MAX_METADATA_BYTES} bytes: {path}")
    content = json.loads(
        content.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant
    )
    if not isinstance(content, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    _check_json(content)
    return content


def _check_json(value: Any) -> None:
    """Validate bounded JSON with traversal storage proportional to nesting."""
    active: set[int] = set()
    text_size = 0

    def visit(item, depth=0):
        nonlocal text_size
        if depth > MAX_METADATA_DEPTH:
            raise ValueError(f"Metadata nesting exceeds {MAX_METADATA_DEPTH} levels.")
        if isinstance(item, (dict, list)):
            if id(item) in active:
                raise ValueError("Metadata must not contain circular references.")
            active.add(id(item))
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("Metadata object keys must be strings.")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(id(item))
        elif isinstance(item, str):
            if len(item) > MAX_METADATA_BYTES:
                raise ValueError("Metadata text exceeds the size limit.")
            try:
                text_size += len(item.encode("utf-8"))
            except UnicodeError as error:
                raise ValueError(
                    "Metadata text must be valid UTF-8 without lone surrogates."
                ) from error
            if text_size > MAX_METADATA_BYTES:
                raise ValueError("Metadata text exceeds the size limit.")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Out of range float: metadata numbers must be finite.")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ValueError(f"Unsupported metadata value type: {type(item).__name__}")

    visit(value)
    size = 0
    for token in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(token.encode("utf-8"))
        if size > MAX_METADATA_BYTES:
            raise ValueError("Metadata exceeds the size limit.")


def validate_annotations(value: Any) -> dict:
    """Return a detached annotation envelope, or reject malformed input early."""
    if not isinstance(value, dict):
        raise ValueError("Annotations must be a JSON object.")
    _check_json(value)
    if "bestmeta" in value or "annotations" in value:
        unknown = set(value) - {"bestmeta", "annotations"}
        if unknown:
            raise ValueError(f"Unknown annotation envelope fields: {sorted(unknown)}")
        bestmeta, custom = value.get("bestmeta", {}), value.get("annotations", {})
        if not isinstance(bestmeta, dict) or not isinstance(custom, dict):
            raise ValueError("bestmeta and annotations must be JSON objects.")
    else:
        bestmeta, custom = value, {}
    return {"bestmeta": deepcopy(bestmeta), "annotations": deepcopy(custom)}


def schema_provenance() -> dict:
    """Identify the verified local schema used by every aligned record."""
    schema = _pinned_schema()
    return {
        "id": schema["$id"],
        "version": schema["version"],
        "canonical_json_sha256": SCHEMA_SHA256,
        "source": "https://github.com/Helmholtz-UFZ/BeStMeta",
        "commit": SCHEMA_COMMIT,
    }


_ACQUISITION = "hardware_and_acquisition.acquisition"
_DIRECT_FIELDS = {
    "width": "video_resolution_width",
    "height": "video_resolution_height",
    "frame_rate": "frame_rate",
    "frame_count": "total_frame_count",
    "bit_depth": "bit_depth",
    "color_mode": "color_mode",
}
_CODECS = {
    "h264": "h264",
    "hevc": "h265",
    "h265": "h265",
    "ffv1": "ffv1",
    "av1": "av1",
    "mjpeg": "mjpeg",
    "mpeg4": "mpeg4",
    "raw": "raw",
    "rawvideo": "raw",
    "other": "other",
}
_CONTAINERS = {"mp4", "avi", "hdf5", "mov", "image_seq", "other"}

_POSITIVE_FIELDS = frozenset(
    """
    frame_rate video_resolution_width video_resolution_height total_frame_count bit_depth
    spatial_resolution illumination_wavelength camera_count camera_distance
    field_of_view_height field_of_view_width lens_focal_length objective_magnification
    arena_diameter arena_height arena_length arena_width plate_well_count
    n_individuals_represented n_individuals_per_arena
    """.split()
)

_NONNEGATIVE_FIELDS = frozenset(
    """
    exposure_time illumination_illuminance water_depth exposure_concentration
    exposure_duration habituation_duration age_value developmental_stage_value
    body_length_value weight_value n_individuals_total n_individuals_start n_individuals_end
    subjects_per_field_of_view dropped_frames_count n_bodyparts_tracked
    n_individuals_tracked_per_arena
    """.split()
)

_PERCENT_FIELDS = frozenset(
    """
    humidity frames_without_tracked_individual frames_without_tracked_bodypart
    """.split()
)

_PROBABILITY_FIELDS = frozenset(
    """
    significance_level tracking_confidence_threshold
    """.split()
)


def _issue(code: str, message: str, severity: str = "error", **details: Any) -> dict:
    issue = {"severity": severity, "code": code, "message": message, "stage": "align"}
    if details:
        issue["details"] = details
    return issue


def _path(parts: Any) -> str:
    return ".".join(str(part) for part in parts)


@lru_cache(maxsize=1)
def _pinned_schema() -> dict:
    schema = read_json(Path(__file__).with_name("bestmeta_schema.json"))
    canonical = json.dumps(
        schema, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != SCHEMA_SHA256:
        raise ValueError("Pinned BeStMeta schema checksum mismatch.")
    Draft201909Validator.check_schema(schema)
    return schema


def _shape(schema: dict, root: dict) -> dict:
    """Resolve the direct or nullable definition references in the pinned schema."""
    reference = schema.get("$ref") or next(
        (choice["$ref"] for choice in schema.get("anyOf", []) if "$ref" in choice), None
    )
    return root["$defs"][reference.removeprefix("#/$defs/")] if reference else schema


def _supplemental_errors(value: Any, schema: dict, root: dict, parts: tuple = ()) -> Iterator[dict]:
    """Catch unknown slots and scientific numeric constraints absent upstream."""
    shape = _shape(schema, root)
    if isinstance(value, dict):
        properties = shape.get("properties", {})
        for key, child in value.items():
            path = (*parts, key)
            if key not in properties:
                yield {
                    "path": _path(path),
                    "code": "unknown_property",
                    "message": "Unknown BeStMeta field; put custom information in annotations.",
                }
            else:
                yield from _supplemental_errors(child, properties[key], root, path)
    elif isinstance(value, list):
        items = shape.get("items", {})
        if isinstance(items, dict):
            for index, child in enumerate(value):
                yield from _supplemental_errors(child, items, root, (*parts, index))
    elif isinstance(value, str) and not value.strip() and "string" in _types(shape):
        # Empty text is not useful scientific metadata, even where upstream
        # permits it. Optional unknown values should be omitted or set to null.
        yield {
            "path": _path(parts),
            "code": "empty_string",
            "message": "Metadata strings must not be empty or whitespace-only.",
        }
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        error = _numeric_error(value, parts[-1] if parts else None)
        if error:
            yield {"path": _path(parts), **error}


def _numeric_error(value, field):
    if isinstance(value, float) and not math.isfinite(value):
        return {
            "code": "nonfinite_number",
            "message": "Scientific metadata must contain finite numbers.",
        }
    if field in _POSITIVE_FIELDS and value <= 0:
        return {
            "code": "nonpositive_number",
            "message": "This physical quantity or structural count must be greater than zero.",
        }
    if field in _NONNEGATIVE_FIELDS and value < 0:
        return {
            "code": "negative_number",
            "message": "This measured magnitude or count must not be negative; zero is permitted.",
        }
    if field in _PERCENT_FIELDS and not 0 <= value <= 100:
        return {
            "code": "percentage_out_of_range",
            "message": "A percentage must be between 0 and 100 inclusive.",
        }
    if field in _PROBABILITY_FIELDS and not 0 <= value <= 1:
        return {
            "code": "probability_out_of_range",
            "message": "A probability or confidence threshold must be between 0 and 1 inclusive.",
        }
    return None


def _types(schema: dict) -> list[str]:
    value = schema.get("type", [])
    return [value] if isinstance(value, str) else value


def _leaf_errors(error: Any) -> Iterator[Any]:
    if error.context:
        for child in error.context:
            yield from _leaf_errors(child)
    else:
        yield error


def _scientific_required(
    validator: Any, required: list, instance: Any, schema: dict
) -> Iterator[Any]:
    """Evaluate active requirements as usable values, beyond key presence.

    Upstream nullable properties otherwise satisfy conditional ``required``
    rules with null values. Extending the validator also respects alternatives:
    a usable age satisfies an anyOf even if another age representation is null.
    """
    yield from Draft201909Validator.VALIDATORS["required"](validator, required, instance, schema)
    if not isinstance(instance, dict):
        return
    for key in required:
        if key not in instance:
            continue
        value = instance[key]
        empty = value is None or (isinstance(value, (str, list, dict)) and not value)
        empty = empty or (isinstance(value, str) and not value.strip())
        if empty:
            yield ValidationError(
                f"Required scientific field {key!r} is null or empty.",
                validator="scientific_required_empty",
                validator_value=key,
                path=[key],
            )


_ScientificValidator = validators.extend(Draft201909Validator, {"required": _scientific_required})


def _has_empty_value(value: Any) -> bool:
    if isinstance(value, (dict, list)):
        children = value.values() if isinstance(value, dict) else value
        return not value or any(_has_empty_value(child) for child in children)
    return value is None or (isinstance(value, str) and not value.strip())


def _validate(bestmeta: dict, schema: dict) -> tuple[list[dict], list[str]]:
    errors, missing = [], set()
    for validator_class, scientific in (
        (Draft201909Validator, False),
        (_ScientificValidator, True),
    ):
        # This second pass can add only null/empty-required diagnostics.
        if scientific and not _has_empty_value(bestmeta):
            continue
        validator = validator_class(schema, format_checker=_FORMATS, registry=_REGISTRY)
        for outer in validator.iter_errors(bestmeta):
            for error in _leaf_errors(outer):
                empty = error.validator == "scientific_required_empty"
                if scientific and not empty:
                    continue
                if (
                    error.validator == "type"
                    and error.validator_value == "null"
                    and isinstance(error.instance, dict)
                ):
                    continue  # The failing nullable branch adds no diagnosis.
                path = _path(error.absolute_path)
                if empty:
                    missing.add(path)
                elif error.validator == "required" and isinstance(error.instance, dict):
                    missing.update(
                        ".".join(filter(None, (path, key)))
                        for key in error.validator_value
                        if key not in error.instance
                    )
                errors.append(
                    {
                        "path": path,
                        "code": str(error.validator),
                        "message": error.message,
                        "schema_path": _path(error.absolute_schema_path),
                    }
                )
    errors.extend(_supplemental_errors(bestmeta, schema, schema))
    errors.extend(_temporal_errors(bestmeta))
    unique = {json.dumps(error, sort_keys=True): error for error in errors}
    return sorted(unique.values(), key=lambda e: (e["path"], e["code"], e["message"])), sorted(
        missing
    )


def _temporal_errors(bestmeta: dict) -> Iterator[dict]:
    for parents, prefix in (
        (("hardware_and_acquisition", "acquisition"), "recording"),
        (("experimental_conditions", "experiment"), "experiment"),
    ):
        container: Any = bestmeta
        for component in parents:
            container = container.get(component, {}) if isinstance(container, dict) else {}
        if not isinstance(container, dict):
            continue
        parsed: dict[str, tuple[datetime, str]] = {}
        for endpoint in ("start", "end"):
            key = f"{prefix}_{endpoint}_datetime"
            value = container.get(key)
            if value is None:
                continue  # Optional unknown timestamps may remain null.
            path = _path((*parents, key))
            valid = isinstance(value, str) and _FORMATS.conforms(value, "date-time")
            try:
                stamp = (
                    datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
                    if valid
                    else None
                )
            except (ValueError, OverflowError, TypeError):
                stamp = None
            if stamp is None:
                yield {
                    "path": path,
                    "code": "scientific_datetime_format",
                    "message": "Use an RFC 3339 timestamp with an explicit UTC offset or Z; local clock times are ambiguous.",
                }
            else:
                # datetime truncates after microseconds; compare the original
                # decimal digits separately so even finer timing stays exact.
                fraction = re.search(r"\.([0-9]+)", value)
                parsed[endpoint] = (
                    stamp.replace(microsecond=0),
                    fraction[1].rstrip("0") if fraction else "",
                )
        if "start" in parsed and "end" in parsed and parsed["end"] < parsed["start"]:
            yield {
                "path": _path((*parents, f"{prefix}_end_datetime")),
                "code": "temporal_order",
                "message": f"{prefix.capitalize()} end precedes {prefix} start.",
            }


def _duration(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("A duration cannot be a boolean.")
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Duration must be a finite nonnegative number.") from error
    if not seconds.is_finite() or seconds < 0:
        raise ValueError("Duration must be a finite nonnegative number.")
    rendered = "0" if seconds.is_zero() else format(seconds, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"PT{rendered}S"


def _exact_duration(value: str) -> str | None:
    """Use an exact decimal only when the rational's expansion terminates."""
    if (
        not isinstance(value, str)
        or len(value) > 1024
        or re.fullmatch(r"\d+(?:/\d+)?", value, re.ASCII) is None
    ):
        raise ValueError("Invalid exact duration rational.")
    try:
        seconds = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Invalid exact duration rational.") from error
    denominator = seconds.denominator
    powers = []
    for factor in (2, 5):
        count = 0
        while denominator % factor == 0:
            denominator //= factor
            count += 1
        powers.append(count)
    if denominator != 1:
        return None  # Keep the shortest float projection and original rational.
    with localcontext() as context:
        context.prec = len(str(seconds.numerator)) + max(powers) + 1
        return _duration(Decimal(seconds.numerator) / Decimal(seconds.denominator))


def _elapsed_duration(value: Any) -> Fraction | None:
    """Compare fixed elapsed ISO durations without calendar/month assumptions."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = re.fullmatch(
        r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        value,
        re.ASCII,
    )
    if match is None or not any(part is not None for part in match.groups()):
        return None
    weeks, days, hours, minutes, seconds = (Fraction(part or "0") for part in match.groups())
    return weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds


def _set(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = deepcopy(value)


def _equal(left: Any, right: Any, path: str = "") -> bool:
    # JSON bools must never compare equal to scientific integer measurements.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if path == f"{_ACQUISITION}.recording_duration":
        left_duration, right_duration = (
            _elapsed_duration(left),
            _elapsed_duration(right),
        )
        if left_duration is not None and right_duration is not None:
            return left_duration == right_duration
    return left == right


_MP4_BRANDS = frozenset("isom iso2 iso3 iso4 iso5 iso6 iso7 iso8 iso9 mp41 mp42 avc1 M4V".split())


def _duration_observation(technical: dict) -> tuple:
    rational = technical.get("duration_rational")
    exact = _exact_duration(rational) if rational is not None else None
    if exact is not None:
        return exact, "technical.duration_rational", "exact_rational_seconds_to_ISO8601_duration"
    return (
        _duration(technical["duration_seconds"]),
        "technical.duration_seconds",
        "seconds_to_ISO8601_duration",
    )


def _container_observation(extraction: dict) -> tuple:
    raw = extraction["technical"]["container"].lower()
    container = raw if raw in _CONTAINERS else "other"
    source, method = "technical.container", "BeStMeta_controlled_vocabulary"
    if {"mov", "mp4"}.issubset(raw.split(",")):
        raw_format = extraction.get("raw", {}).get("format", {})
        tags = raw_format.get("tags", {}) if isinstance(raw_format, dict) else {}
        brand = tags.get("major_brand") if isinstance(tags, dict) else None
        brand = brand.strip() if isinstance(brand, str) else None
        container = "mov" if brand == "qt" else "mp4" if brand in _MP4_BRANDS else "other"
        if container != "other":
            source = "raw.format.tags.major_brand"
            method = "registered_ISOBMFF_major_brand_to_BeStMeta_container"
    return container, source, method


def _measurements(extraction: dict, issues: list) -> Iterator[tuple]:
    """Project only justified measurements, retaining their source and method."""
    technical = extraction.get("technical", {})
    digest = extraction.get("asset", {}).get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        yield (
            "dataset_id",
            "urn:sha256:" + digest.lower(),
            "asset.sha256",
            "generated_file_view_identifier",
        )
    for source, destination in _DIRECT_FIELDS.items():
        if technical.get(source) is not None:
            yield (
                f"{_ACQUISITION}.{destination}",
                technical[source],
                f"technical.{source}",
                "direct",
            )
    if technical.get("duration_seconds") is not None:
        try:
            yield f"{_ACQUISITION}.recording_duration", *_duration_observation(technical)
        except ValueError as error:
            issues.append(
                _issue("invalid_duration", str(error), value=repr(technical["duration_seconds"]))
            )
    codec = technical.get("codec")
    if isinstance(codec, str) and codec:
        mapped = _CODECS.get(codec.lower(), "other")
        yield (
            f"{_ACQUISITION}.video_codec",
            mapped,
            "technical.codec",
            "BeStMeta_controlled_vocabulary",
        )
        if mapped == "other" and codec.lower() != "other":
            issues.append(
                _issue(
                    "codec_not_in_vocabulary",
                    "Codec is retained in the raw extraction and represented as 'other' in BeStMeta.",
                    "warning",
                    codec=codec.lower(),
                )
            )
    container = technical.get("container")
    if isinstance(container, str) and container:
        mapped, source, method = _container_observation(extraction)
        yield f"{_ACQUISITION}.video_container_format", mapped, source, method
        if mapped == "other" and container.lower() != "other":
            issues.append(
                _issue(
                    "container_not_in_vocabulary",
                    "Container family is retained in the raw extraction and represented as 'other' in BeStMeta.",
                    "warning",
                    container=container.lower(),
                )
            )


def _evidence(source: str, path: str, value: Any, method: str) -> dict:
    return {
        "source": source,
        "source_path": path,
        "method": method,
        "value": deepcopy(value),
        "selected": True,
    }


class _Alignment:
    """The mutable state of one record, confined to measurement and merge steps."""

    def __init__(self, issues: list):
        self.bestmeta: dict = {}
        self.provenance: dict = {}
        self.conflicts: list = []
        self.issues = deepcopy(issues)

    def measure(self, path: str, value: Any, source: str, method: str) -> None:
        _set(self.bestmeta, path, value)
        self.provenance[path] = [_evidence("extracted", source, value, method)]

    def _conflict(self, path: str, existing: Any, candidate: Any, history: list) -> None:
        if not history:
            history.append(_evidence("extracted", path, existing, "aggregate_extracted_subtree"))
        self.conflicts.append(
            {
                "path": path,
                "extracted": deepcopy(existing),
                "annotation": deepcopy(candidate),
            }
        )
        self.issues.append(
            _issue(
                "metadata_conflict",
                f"Conflicting extracted and annotated values for {path}.",
                path=path,
            )
        )

    def merge(self, incoming: dict, destination: dict | None = None, prefix: tuple = ()) -> None:
        destination = self.bestmeta if destination is None else destination
        for key, candidate in incoming.items():
            existing = destination.get(key)
            if isinstance(candidate, dict) and (
                key not in destination or isinstance(existing, dict)
            ):
                self.merge(candidate, destination.setdefault(key, {}), (*prefix, key))
                continue
            path = _path((*prefix, key))
            history = self.provenance.setdefault(path, [])
            evidence = _evidence("annotation", path, candidate, "researcher_supplied")
            conflict = (
                key in destination
                and path != "dataset_id"
                and not _equal(existing, candidate, path)
            )
            if conflict:
                self._conflict(path, existing, candidate, history)
                evidence["selected"] = False
                history.append(evidence)
                continue
            for prior in history:
                prior["selected"] = False
            history.append(evidence)
            destination[key] = deepcopy(candidate)


def align_metadata(extraction: dict, annotations: dict | None = None) -> dict:
    """Align measurements and context; conflicting annotations never overwrite evidence."""
    active_schema = _pinned_schema()
    state = _Alignment(extraction.get("issues", []))
    for measurement in _measurements(extraction, state.issues):
        state.measure(*measurement)
    try:
        envelope = validate_annotations({} if annotations is None else annotations)
    except (ValueError, RecursionError) as error:
        state.issues.append(_issue("invalid_annotations", str(error)))
        envelope = {"bestmeta": {}, "annotations": {}}
    state.merge(envelope["bestmeta"])
    errors, missing = _validate(state.bestmeta, active_schema)
    if errors:
        state.issues.append(
            _issue(
                "bestmeta_validation_failed",
                f"Metadata has {len(errors)} schema or scientific validation error(s).",
                missing_required=missing,
            )
        )
    valid = not errors and not any(i.get("severity") == "error" for i in state.issues)
    return {
        "extraction": deepcopy(extraction),
        "bestmeta": state.bestmeta,
        "annotations": envelope["annotations"],
        "provenance": state.provenance,
        "conflicts": state.conflicts,
        "issues": state.issues,
        "validation": {"valid": valid, "errors": errors, "missing_required": missing},
    }

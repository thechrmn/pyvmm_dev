"""Stage 3 - align file measurements and researcher context with a metadata schema.

BeStMeta (https://github.com/Helmholtz-UFZ/BeStMeta) is the default framework: its
JSON Schema is bundled in ``schemas/`` and pinned by SHA-256. Any other JSON Schema
can be selected, but ``FIELD_MAP`` then needs the matching destinations.

Rules that keep records honest:

* A measured value is never replaced by researcher input; a disagreement is recorded
  as a conflict. Playback timing (``PLAYBACK_KEYS``) is the exception, because capture
  and playback rates differ for time-lapse and high-speed recordings.
* A measured value the schema cannot name (e.g. codec ``vp9``) becomes ``other`` when
  the schema's enum offers it; the measured name stays in ``extraction.technical``.
* Validation problems never discard evidence; they make the record a draft.
"""

import base64
import contextlib
import hashlib
import json
import math
import re
import stat
from copy import deepcopy
from datetime import date
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

DEFAULT_SCHEMA = Path(__file__).resolve().parent / "schemas" / "bestmeta_schema.json"
MAX_JSON_BYTES = 16 * 1024 * 1024
_ACQUISITION = ("hardware_and_acquisition", "acquisition")
FIELD_MAP = {
    "width": (*_ACQUISITION, "video_resolution_width"),
    "height": (*_ACQUISITION, "video_resolution_height"),
    "frame_rate": (*_ACQUISITION, "frame_rate"),
    "duration_seconds": (*_ACQUISITION, "recording_duration"),
    "frame_count": (*_ACQUISITION, "total_frame_count"),
    "bit_depth": (*_ACQUISITION, "bit_depth"),
    "color_mode": (*_ACQUISITION, "color_mode"),
    "codec": (*_ACQUISITION, "video_codec"),
    "container": (*_ACQUISITION, "video_container_format"),
}
PLAYBACK_KEYS = frozenset({"frame_rate", "duration_seconds"})
_MP4_BRANDS = frozenset("isom iso2 iso3 iso4 iso5 iso6 iso7 iso8 iso9 mp41 mp42 avc1 M4V".split())
_CODEC_ALIASES = {"hevc": "h265", "rawvideo": "raw"}

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-]([0-9]{2}):([0-9]{2}))"
)
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*")


# --- format checks: fixed and strict, so results never depend on installed extras ---
def _is_date(value):
    if not isinstance(value, str):
        return True
    try:
        return bool(_DATE.fullmatch(value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def _is_datetime(value):
    """RFC 3339 date-time (leap second allowed, offset required)."""
    if not isinstance(value, str):
        return True
    match = _DATETIME.fullmatch(value)
    if not match:
        return False
    year, month, day, hour, minute, second, off_h, off_m = (
        int(g) if g is not None else 0 for g in match.groups()
    )
    try:
        date(year, month, day)
    except ValueError:
        return False
    return hour < 24 and minute < 60 and second < 61 and off_h < 24 and off_m < 60


def _is_uri(value):
    """RFC 3986 syntax: scheme, permitted characters and well-formed percent escapes."""
    if not isinstance(value, str):
        return True
    return bool(_URI.fullmatch(value)) and not re.search(r"%(?![0-9A-Fa-f]{2})", value)


# --- reading JSON ---
def _read_bytes(path):
    path = Path(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Not a regular file: {path}")
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError(f"File exceeds {MAX_JSON_BYTES} bytes: {path}")
    return data


def _parse(data):
    """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity, finite numbers only."""

    def unique(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate object key.")
        return dict(pairs)

    def reject(token):
        raise ValueError(f"Non-finite number: {token}")

    def finite(token):
        number = float(token)
        if not math.isfinite(number):
            raise ValueError(f"Number out of range: {token}")
        return number

    try:
        return json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=unique,
            parse_constant=reject,
            parse_float=finite,
        )
    except RecursionError as error:
        raise ValueError("JSON is nested too deeply.") from error


def load_metadata(path=None):
    """Read researcher metadata as ``{"bestmeta": {...}, "annotations": {...}}``.

    The file is one JSON object: either BeStMeta fields directly, or an envelope whose
    only keys are ``bestmeta`` and ``annotations`` (free-form extra context).
    Raises ValueError when the file is unreadable or malformed.
    """
    if path is None:
        return {"bestmeta": {}, "annotations": {}}
    try:
        value = _parse(_read_bytes(path))
    except ValueError as error:
        raise ValueError(f"Invalid metadata file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Invalid metadata file {path}: the root must be a JSON object.")
    if "bestmeta" in value or set(value) == {"annotations"}:
        envelope = {name: value.get(name, {}) for name in ("bestmeta", "annotations")}
        if set(value) - set(envelope) or not all(isinstance(v, dict) for v in envelope.values()):
            raise ValueError(
                f"Invalid metadata file {path}: an envelope has only the object fields "
                "'bestmeta' and 'annotations'."
            )
        return envelope
    return {"bestmeta": value, "annotations": {}}


# --- schema ---
def _issue(code, message, severity="warning"):
    """Alignment findings are warnings: they affect ``validation``, not the trust in the evidence."""
    return {"severity": severity, "code": code, "message": message, "stage": "align"}


def load_schema(path=None):
    """Load the JSON Schema used for validation (default: the bundled BeStMeta schema).

    Never raises: a missing ``jsonschema`` package or an unusable schema yields
    ``issues`` and no validator, so extracted evidence is still exported as drafts.
    Remote ``$ref`` retrieval is disabled. Returns ``info``, ``schema``,
    ``validator`` and ``issues``.
    """
    path = DEFAULT_SCHEMA if path is None else Path(path).expanduser()
    info = {"path": str(path), "sha256": None, "dialect": None, "status": "unavailable"}
    context = {"info": info, "schema": None, "validator": None, "issues": []}
    try:
        from jsonschema import FormatChecker, validators
        from referencing import Registry
    except ImportError:
        context["issues"].append(_issue("jsonschema_not_installed", "Install 'jsonschema' to validate."))
        return context
    try:
        data = _read_bytes(path)
        info["sha256"] = hashlib.sha256(data).hexdigest()
        schema = _parse(data)
        selected = (
            validators.validator_for(schema, default=None)
            if isinstance(schema, dict) and "$schema" in schema
            else validators.validator_for(schema)
        )
        if selected is None:
            raise ValueError("Unsupported JSON Schema dialect.")
        selected.check_schema(schema)
        checker = FormatChecker(formats=())
        for name, check in (("date", _is_date), ("date-time", _is_datetime), ("uri", _is_uri)):
            checker.checks(name)(check)
        context["validator"] = selected(schema, format_checker=checker, registry=Registry())
        context["schema"] = schema
        info["dialect"] = selected.META_SCHEMA.get("$id", selected.META_SCHEMA.get("id"))
        info["status"] = "available"
    except Exception as error:  # noqa: BLE001 - any schema problem must degrade to drafts
        context["issues"].append(_issue("schema_unavailable", f"{type(error).__name__}: {error}"))
    return context


def _path(parts):
    """Dotted path; '~' and '.' inside a key are escaped as '~0' and '~1'."""
    return ".".join(str(part).replace("~", "~0").replace(".", "~1") for part in parts)


def _leaves(error):
    """Most specific errors, looking through anyOf/oneOf.

    A branch that fails only on the kind of value (such as the ``null`` alternative of
    an optional property) says nothing useful and is dropped.
    """
    if not error.context:
        yield error
        return
    informative = [c for c in error.context if not (c.validator == "type" and not c.relative_path)]
    for child in informative or error.context:
        yield from _leaves(child)


def _fit_enum(context, destination, value):
    """Return ``other`` if the schema's enum at ``destination`` rejects ``value`` but offers it."""
    if context["validator"] is None:
        return value
    probe = value
    for part in reversed(destination):
        probe = {part: probe}
    with contextlib.suppress(Exception):  # unresolvable schema parts surface in the record's own validation
        for outer in context["validator"].iter_errors(probe):
            for error in _leaves(outer):
                if (
                    error.validator == "enum"
                    and tuple(error.absolute_path) == destination
                    and "other" in error.validator_value
                ):
                    return "other"
    return value


# --- measurements ---
def _iso_duration(seconds):
    """ISO 8601 duration with microsecond resolution: 4.933333333 -> 'PT4.933333S'."""
    micros = round(Fraction(seconds) * 1_000_000)
    hours, rest = divmod(micros, 3_600_000_000)
    minutes, rest = divmod(rest, 60_000_000)
    whole, fraction = divmod(rest, 1_000_000)
    text = "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    if whole or fraction or not (hours or minutes):
        text += str(whole) + (f".{fraction:06d}".rstrip("0") if fraction else "") + "S"
    return text


def _container(technical, raw):
    """BeStMeta container name. FFprobe gives one name to the whole ISO base-media family
    (mp4, mov, 3gp, mj2, ...), so the registered major brand decides between mov and mp4."""
    name = technical["container"].lower()
    if {"mov", "mp4"} <= set(name.split(",")):
        brand = str(raw.get("format", {}).get("tags", {}).get("major_brand", "")).strip()
        if brand == "qt":
            return "mov", "raw.format.tags.major_brand", "ISOBMFF_major_brand"
        if brand in _MP4_BRANDS:
            return "mp4", "raw.format.tags.major_brand", "ISOBMFF_major_brand"
    return name, "technical.container", "direct"


def _check_field_map(field_map):
    destinations = list(field_map.values())
    if any(
        not isinstance(d, tuple) or not d or not all(isinstance(p, str) for p in d)
        for d in destinations
    ):
        raise ValueError("Mapping destinations must be non-empty tuples of strings.")
    ordered = sorted([("dataset_id",), *destinations])
    if any(right[: len(left)] == left for left, right in pairwise(ordered)):
        raise ValueError("Mapping destinations must not overlap.")


def _measurements(extraction, context, field_map):
    """Yield (destination, value, source, method, replaceable) for every usable measurement."""
    technical, raw = extraction.get("technical", {}), extraction.get("raw", {})
    digest = extraction.get("asset", {}).get("sha256")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        identifier = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode("ascii")
        yield ("dataset_id",), f"ni:///sha-256;{identifier}", "asset.sha256", "generated_file_view_identifier", True
    for key, destination in field_map.items():
        value = technical.get(key)
        if value is None:
            continue
        source, method = f"technical.{key}", "direct"
        if key == "duration_seconds":
            value, method = _iso_duration(value), "seconds_to_ISO_8601_duration"
        elif key == "codec":
            value = value.lower()
            value, method = _CODEC_ALIASES.get(value, value), "codec_name_to_BeStMeta"
        elif key == "container":
            value, source, method = _container(technical, raw)
        if isinstance(value, str):
            fitted = _fit_enum(context, destination, value)
            method += "+other" if fitted != value else ""
            value = fitted
        yield destination, value, source, method, key in PLAYBACK_KEYS


def _same_json(measured, supplied):
    """Equality for a measured scalar: numeric forms agree (30 == 30.0), booleans stay distinct."""
    return isinstance(measured, bool) == isinstance(supplied, bool) and measured == supplied


def _evidence(source, path, value, method, selected=True):
    return {
        "source": source,
        "source_path": path,
        "method": method,
        "value": deepcopy(value),
        "selected": selected,
    }


def _combine(measurements, researcher):
    """Place measurements, then merge researcher values; return (bestmeta, provenance, conflicts)."""
    bestmeta, provenance, conflicts, replaceable = {}, {}, [], set()
    for parts, value, source, method, replace in measurements:
        target = bestmeta
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
        provenance[_path(parts)] = [_evidence("extracted", source, value, method)]
        if replace:
            replaceable.add(_path(parts))

    def merge(incoming, target, prefix=()):
        for key, value in incoming.items():
            existing = target.get(key)
            if isinstance(value, dict) and (key not in target or isinstance(existing, dict)):
                merge(value, target.setdefault(key, {}), (*prefix, key))
                continue
            path = _path((*prefix, key))
            history = provenance.setdefault(path, [])
            conflict = key in target and not _same_json(existing, value) and path not in replaceable
            if conflict:
                if not history:
                    history.append(_evidence("extracted", path, existing, "aggregate_extracted_subtree"))
                conflicts.append({"path": path, "extracted": deepcopy(existing), "annotation": deepcopy(value)})
            else:
                for prior in history:
                    prior["selected"] = False
                target[key] = deepcopy(value)
            history.append(_evidence("annotation", path, value, "researcher_supplied", not conflict))

    merge(researcher, bestmeta)
    return bestmeta, provenance, conflicts


def _validate(context, bestmeta):
    """Return (errors, missing_required_paths) flattened across anyOf/oneOf branches."""
    errors, missing = [], set()
    for outer in context["validator"].iter_errors(bestmeta):
        for error in _leaves(outer):
            errors.append(
                {
                    "path": _path(error.absolute_path),
                    "code": str(error.validator),
                    "message": error.message[:500],
                    "schema_path": _path(error.absolute_schema_path),
                }
            )
            if error.validator == "required":
                missing.update(
                    _path((*error.absolute_path, key))
                    for key in error.validator_value
                    if key not in error.instance
                )
    errors.sort(key=lambda e: (e["path"], e["code"], e["message"]))
    return errors, sorted(missing)


def _check(context, bestmeta, issues):
    """Validate; return (schema_valid, status, errors, missing_required). None means unvalidated."""
    status = context["info"]["status"]
    if context["validator"] is not None:
        try:
            errors, missing = _validate(context, bestmeta)
        except Exception as error:  # noqa: BLE001 - e.g. an unresolvable $ref; evidence must survive
            status = "unavailable"
            issues.append(_issue("schema_validation_unavailable", f"{type(error).__name__}: {error}"))
        else:
            if errors:
                issues.append(_issue("bestmeta_validation_failed", f"Metadata has {len(errors)} schema errors."))
            return not errors, status, errors, missing
    unavailable = {
        "path": "",
        "code": "schema_unavailable",
        "message": "Schema validation is unavailable; evidence is retained.",
        "schema_path": "",
    }
    return None, status, [unavailable], []


def align_metadata(extraction, metadata=None, schema=None, field_map=None):
    """Align one extraction with researcher metadata and validate the result.

    ``metadata`` comes from ``load_metadata`` and ``schema`` from ``load_schema``
    (loaded once per run; None means no researcher input / the default schema).
    ``field_map`` maps technical keys to destination tuples (default ``FIELD_MAP``).
    Inputs are never modified. Returns the record described in README.md.
    """
    metadata = metadata or {"bestmeta": {}, "annotations": {}}
    schema = load_schema() if schema is None else schema
    field_map = FIELD_MAP if field_map is None else field_map
    _check_field_map(field_map)
    bestmeta, provenance, conflicts = _combine(
        _measurements(extraction, schema, field_map), metadata["bestmeta"]
    )
    issues = deepcopy(extraction.get("issues", [])) + deepcopy(schema["issues"])
    issues += [_issue("metadata_conflict", f"Conflicting metadata at {c['path']}.") for c in conflicts]
    schema_valid, status, errors, missing = _check(schema, bestmeta, issues)
    return {
        "extraction": {key: deepcopy(extraction.get(key, {})) for key in ("asset", "raw", "technical")},
        "bestmeta": bestmeta,
        "annotations": deepcopy(metadata["annotations"]),
        "provenance": provenance,
        "conflicts": conflicts,
        "issues": issues,
        "validation": {
            "valid": schema_valid is True
            and not conflicts
            and not any(issue["severity"] == "error" for issue in issues),
            "schema_valid": schema_valid,
            "schema_status": status,
            "errors": errors,
            "missing_required": missing,
        },
    }

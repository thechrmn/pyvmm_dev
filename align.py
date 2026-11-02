"""Map file measurements and researcher context; validate with an optional schema."""

import base64
import hashlib
import json
import math
import os
import re
import stat
from copy import deepcopy
from datetime import date
from pathlib import Path

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_DEPTH = 64


def _format_checker():
    from jsonschema import FormatChecker

    formats = FormatChecker()

    @formats.checks("date")
    def _date(value):
        if not isinstance(value, str):
            return True
        try:
            return bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)) and bool(
                date.fromisoformat(value)
            )
        except ValueError:
            return False

    @formats.checks("date-time")
    def _datetime(value):
        if not isinstance(value, str):
            return True
        from rfc3339_validator import validate_rfc3339

        return (
            value.isascii() and not value.endswith("\n") and bool(validate_rfc3339(value.upper()))
        )

    @formats.checks("uri")
    def _uri(value):
        if not isinstance(value, str):
            return True
        from rfc3986_validator import validate_rfc3986

        match = validate_rfc3986(value, rule="URI")
        return match is not None and match.end() == len(value)

    return formats


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Nonfinite JSON number: {value}")


def _float(token):
    value = float(token)
    if not math.isfinite(value) or (
        value == 0 and any(c in "123456789" for c in token.lower().partition("e")[0])
    ):
        raise ValueError("JSON number is nonfinite or underflows to zero.")
    return value


def _check_json(value):
    """Reject non-JSON values, ambiguous keys, cycles and excessive input size/depth."""
    active = set()

    def visit(item, depth=0):
        if depth > MAX_METADATA_DEPTH:
            raise ValueError(f"Metadata exceeds {MAX_METADATA_DEPTH} nesting levels.")
        if isinstance(item, (dict, list)):
            if id(item) in active:
                raise ValueError("Metadata contains a circular reference.")
            active.add(id(item))
            if isinstance(item, dict) and any(not isinstance(key, str) for key in item):
                raise ValueError("Metadata object keys must be strings.")
            for child in item.values() if isinstance(item, dict) else item:
                visit(child, depth + 1)
            active.remove(id(item))
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError(f"Unsupported metadata type: {type(item).__name__}")
        elif isinstance(item, str) and len(item) > MAX_METADATA_BYTES:
            raise ValueError("Metadata text exceeds the size limit.")

    visit(value)
    size = 0
    try:
        for token in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            size += len(token.encode("utf-8"))
            if size > MAX_METADATA_BYTES:
                raise ValueError("Metadata exceeds the size limit.")
    except (TypeError, UnicodeError, OverflowError) as error:
        raise ValueError(f"Metadata must be finite UTF-8 JSON: {error}") from error


def _read_bytes(path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(Path(path).expanduser(), flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            observed = os.fstat(stream.fileno())
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError("Metadata must be a regular file.")
            limit = min(observed.st_size, MAX_METADATA_BYTES) + 1
            content = stream.read(limit)
            if len(content) == limit:
                content += stream.read(MAX_METADATA_BYTES + 1 - limit)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(content) > MAX_METADATA_BYTES:
        raise ValueError("Metadata file exceeds the size limit.")
    return content


def _decode(content):
    value = json.loads(
        content.decode("utf-8-sig"),
        object_pairs_hook=_pairs,
        parse_constant=_constant,
        parse_float=_float,
    )
    _check_json(value)
    return value


def read_json(path: str | Path) -> dict:
    """Read bounded UTF-8 JSON with duplicate-key and numeric-loss checks."""
    value = _decode(_read_bytes(path))
    if not isinstance(value, dict):
        raise ValueError("Metadata JSON root must be an object.")
    return value


def validate_annotations(value, *, bestmeta=True) -> dict:
    """Normalize BeStMeta context, or keep generic metadata literal when selected."""
    if not isinstance(value, dict):
        raise ValueError("Metadata must be a JSON object.")
    _check_json(value)
    if bestmeta and ("bestmeta" in value or set(value) == {"annotations"}):
        if set(value) - {"bestmeta", "annotations"}:
            raise ValueError("Envelope fields must be bestmeta and annotations.")
        result = {
            "metadata": value.get("bestmeta", {}),
            "annotations": value.get("annotations", {}),
        }
        if any(not isinstance(child, dict) for child in result.values()):
            raise ValueError("bestmeta and annotations must be objects.")
    else:
        result = {"metadata": value, "annotations": {}}
    return deepcopy(result)


def _issue(code, message, severity="error"):
    return {"severity": severity, "code": code, "message": message, "stage": "align"}


def _check_schema_resources(resource, selected, resolver=None, seen=None):
    """Check declared dialects and vocabularies in actual schema locations only."""
    from jsonschema import validators
    from referencing import Registry, Resource
    from referencing.jsonschema import specification_with

    resolver = Registry().resolver_with_root(resource) if resolver is None else resolver
    seen = set() if seen is None else seen
    schema = resource.contents
    if isinstance(schema, dict):
        if "$schema" in schema:
            selected = validators.validator_for(schema, default=None)
            if selected is None:
                raise ValueError("Unsupported embedded JSON Schema dialect.")
    key = (id(schema), selected)
    if key in seen:
        return
    seen.add(key)
    selected.check_schema(schema)
    if isinstance(schema, dict):
        declared = schema.get("$vocabulary", {})
        supported = selected.META_SCHEMA.get("$vocabulary", {})
        unknown = [
            uri for uri, required in declared.items() if required is True and uri not in supported
        ]
        if unknown:
            raise ValueError(f"Unsupported required JSON Schema vocabularies: {unknown}")
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword in schema and keyword in selected.VALIDATORS:
                resolved = resolver.lookup(schema[keyword])
                dialect = selected.META_SCHEMA.get("$id", selected.META_SCHEMA.get("id"))
                target = Resource.from_contents(
                    resolved.contents, default_specification=specification_with(dialect)
                )
                _check_schema_resources(target, selected, resolved.resolver, seen)
    for child in resource.subresources():
        _check_schema_resources(child, selected, resolver.in_subresource(child), seen)


def load_schema(schema_path: str | Path | None = None, *, enabled=True) -> dict:
    """Read the selected/default schema; only explicit opt-out skips validation."""
    info = {
        "path": None,
        "file_sha256": None,
        "document": None,
        "status": "unavailable",
        "diagnostics": [],
    }
    context = {"validator": None, "provenance": info}
    if not enabled:
        info["status"] = "not_requested"
        return context
    schema_path = schema_path if schema_path is not None else os.environ.get("PYVMM_SCHEMA")
    if not schema_path:
        info["diagnostics"].append(
            _issue(
                "schema_not_configured",
                "Set PYVMM_SCHEMA to the BeStMeta schema, supply --schema, or opt out with --no-schema.",
            )
        )
        return context
    try:
        path = Path(schema_path).expanduser().absolute()
        info["path"] = str(path)
        content = _read_bytes(path)
        info["file_sha256"] = hashlib.sha256(content).hexdigest()
        schema = _decode(content)
        info["document"] = schema
        if not isinstance(schema, (dict, bool)):
            raise ValueError("A JSON Schema must be an object or boolean.")
        from jsonschema import validators
        from referencing import Registry, Resource
        from referencing.jsonschema import specification_with

        selected = (
            validators.validator_for(schema, default=None)
            if isinstance(schema, dict) and "$schema" in schema
            else validators.validator_for(schema)
        )
        if selected is None:
            raise ValueError("Unsupported JSON Schema dialect.")
        dialect = selected.META_SCHEMA.get("$id", selected.META_SCHEMA.get("id"))
        resource = Resource.from_contents(schema, default_specification=specification_with(dialect))
        _check_schema_resources(resource, selected)
        context.update(
            validator=selected(schema, format_checker=_format_checker(), registry=Registry()),
        )
        info.update(
            status="available",
            dialect=dialect,
        )
    except Exception as error:
        info["diagnostics"].append(_issue("schema_unavailable", f"{type(error).__name__}: {error}"))
    return context


def _path(parts):
    return ".".join(str(part).replace("~", "~0").replace(".", "~1") for part in parts)


def _bestmeta_measurements(extraction):
    technical = extraction.get("technical", {})
    digest = extraction.get("asset", {}).get("sha256", "")
    if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        identifier = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode("ascii")
        yield (
            "dataset_id",
            "ni:///sha-256;" + identifier,
            "asset.sha256",
            "generated_file_view_identifier",
        )
    prefix = "hardware_and_acquisition.acquisition."
    for source, destination in {
        "width": "video_resolution_width",
        "height": "video_resolution_height",
        "frame_count": "total_frame_count",
        "bit_depth": "bit_depth",
        "color_mode": "color_mode",
    }.items():
        if technical.get(source) is not None:
            yield prefix + destination, technical[source], "technical." + source, "direct"
    codec = technical.get("codec")
    if isinstance(codec, str) and codec:
        codec = codec.lower()
        codec = {"hevc": "h265", "rawvideo": "raw"}.get(codec, codec)
        yield (
            prefix + "video_codec",
            codec,
            "technical.codec",
            "codec_name_to_BeStMeta",
        )
    container = technical.get("container")
    if isinstance(container, str) and container:
        container = container.lower()
        source = "technical.container"
        method = "direct"
        if {"mov", "mp4"}.issubset(container.split(",")):
            raw_format = extraction.get("raw", {}).get("format", {})
            tags = raw_format.get("tags", {}) if isinstance(raw_format, dict) else {}
            brand = tags.get("major_brand") if isinstance(tags, dict) else None
            brand = brand.strip() if isinstance(brand, str) else ""
            container = (
                "mov"
                if brand == "qt"
                else "mp4"
                if brand
                in {
                    "isom",
                    "iso2",
                    "iso3",
                    "iso4",
                    "iso5",
                    "iso6",
                    "iso7",
                    "iso8",
                    "iso9",
                    "mp41",
                    "mp42",
                    "avc1",
                    "M4V",
                }
                else container
            )
            if container != technical["container"].lower():
                source = "raw.format.tags.major_brand"
                method = "registered_ISOBMFF_major_brand_to_BeStMeta_container"
        yield (
            prefix + "video_container_format",
            container,
            source,
            method,
        )


def _pointer(path):
    if (
        not isinstance(path, str)
        or (path and not path.startswith("/"))
        or re.search(r"~(?![01])", path)
    ):
        raise ValueError("Mapping paths must be JSON Pointers.")
    parts = (
        tuple(part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/"))
        if path
        else ()
    )
    if len(parts) > MAX_METADATA_DEPTH:
        raise ValueError("Mapping path exceeds the nesting limit.")
    return parts


def validate_mapping(value):
    """Check source-to-object-target pointers, rejecting overlapping destinations."""
    if not isinstance(value, dict):
        raise ValueError("Mapping must be a JSON object of source-to-target pointers.")
    _check_json(value)
    pairs = [(source, _pointer(source), _pointer(target)) for source, target in value.items()]
    targets = sorted(target for _, _, target in pairs)
    if any(not target for target in targets) or any(
        right[: len(left)] == left for left, right in zip(targets, targets[1:])
    ):
        raise ValueError("Mapping targets must be non-root, distinct and non-overlapping.")
    return pairs


def _measurements(extraction, mapping, bestmeta, issues):
    if bestmeta:
        for path, value, source, method in _bestmeta_measurements(extraction):
            yield tuple(path.split(".")), value, "/" + source.replace(".", "/"), method
    elif mapping is None:
        for key, value in extraction.get("technical", {}).items():
            source = "/technical/" + key.replace("~", "~0").replace("/", "~1")
            yield (key,), value, source, "direct"
    else:
        for source, parts, target in validate_mapping(mapping):
            value = extraction
            try:
                for part in parts:
                    if isinstance(value, list):
                        if not re.fullmatch(r"0|[1-9][0-9]*", part):
                            raise KeyError(part)
                        value = value[int(part)]
                    else:
                        value = value[part]
            except (KeyError, IndexError, TypeError, ValueError):
                issues.append(_issue("mapping_source_missing", f"No value at {source}."))
                continue
            yield target, value, source, "explicit_mapping"


def _evidence(source, path, value, method, selected=True):
    return {
        "source": source,
        "source_path": path,
        "method": method,
        "value": deepcopy(value),
        "selected": selected,
    }


def _same_value(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return isinstance(left, bool) == isinstance(right, bool) and left == right


def _errors(error):
    if error.context:
        for child in error.context:
            yield from _errors(child)
    else:
        yield error


def align_metadata(
    extraction: dict,
    annotations=None,
    *,
    schema_path=None,
    schema_context=None,
    mapping=None,
    bestmeta=None,
    use_schema=True,
) -> dict:
    """Preserve observations and context, rejecting silent measurement overwrites."""
    bestmeta = mapping is None if bestmeta is None else bestmeta
    if bestmeta and mapping is not None:
        raise ValueError("Choose BeStMeta or an explicit mapping, not both.")
    if not use_schema:
        context = load_schema(enabled=False)
    else:
        context = load_schema(schema_path) if schema_context is None else schema_context
    info = context["provenance"]
    metadata, provenance, conflicts = {}, {}, []
    issues = deepcopy(extraction.get("issues", [])) + deepcopy(info["diagnostics"])
    for parts, value, source, method in _measurements(extraction, mapping, bestmeta, issues):
        target = metadata
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(value)
        provenance[_path(parts)] = [_evidence("extracted", source, value, method)]
    try:
        envelope = validate_annotations(
            {} if annotations is None else annotations, bestmeta=bestmeta
        )
    except (ValueError, RecursionError) as error:
        envelope = {"metadata": {}, "annotations": {}}
        issues.append(_issue("invalid_annotations", str(error)))

    def merge(incoming, target, prefix=()):
        for key, value in incoming.items():
            existing = target.get(key)
            if isinstance(value, dict) and (key not in target or isinstance(existing, dict)):
                merge(value, target.setdefault(key, {}), (*prefix, key))
                continue
            path = _path((*prefix, key))
            history = provenance.setdefault(path, [])
            conflict = (
                key in target
                and not (bestmeta and path == "dataset_id")
                and not _same_value(existing, value)
            )
            if conflict:
                if not history:
                    history.append(
                        _evidence("extracted", path, existing, "aggregate_extracted_subtree")
                    )
                conflicts.append(
                    {"path": path, "extracted": deepcopy(existing), "annotation": deepcopy(value)}
                )
                issues.append(_issue("metadata_conflict", f"Conflicting metadata at {path}."))
            else:
                for prior in history:
                    prior["selected"] = False
                target[key] = deepcopy(value)
            history.append(
                _evidence("annotation", path, value, "researcher_supplied", not conflict)
            )

    merge(envelope["metadata"], metadata)
    errors, missing, schema_valid = [], set(), None
    schema_status = info["status"]
    if context["validator"] is not None:
        try:
            for outer in context["validator"].iter_errors(metadata):
                for error in _errors(outer):
                    errors.append(
                        {
                            "path": _path(error.absolute_path),
                            "code": str(error.validator),
                            "message": error.message,
                            "schema_path": _path(error.absolute_schema_path),
                        }
                    )
                    if error.validator == "required" and isinstance(error.instance, dict):
                        missing.update(
                            _path((*error.absolute_path, key))
                            for key in error.validator_value
                            if key not in error.instance
                        )
            schema_valid = not errors
        except Exception as error:
            schema_status = "unavailable"
            issues.append(
                _issue("schema_validation_unavailable", f"{type(error).__name__}: {error}")
            )
    if schema_valid is None and schema_status != "not_requested":
        errors.append(
            {
                "path": "",
                "code": "schema_unavailable",
                "message": "Schema validation is unavailable; evidence is retained.",
            }
        )
    elif errors:
        issues.append(
            _issue("schema_validation_failed", f"Metadata has {len(errors)} schema errors.")
        )
    errors.sort(key=lambda error: (error["path"], error["code"], error["message"]))
    clean = not conflicts and not any(i.get("severity") == "error" for i in issues)
    return {
        "extraction": deepcopy(extraction),
        "metadata": metadata,
        "annotations": envelope["annotations"],
        "provenance": provenance,
        "conflicts": conflicts,
        "issues": issues,
        "validation": {
            "ok": clean and (schema_status == "not_requested" or schema_valid is True),
            "valid": None if schema_valid is None else schema_valid and clean,
            "schema_valid": schema_valid,
            "schema_status": schema_status,
            "errors": errors,
            "missing_required": sorted(missing),
        },
    }

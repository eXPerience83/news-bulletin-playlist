"""Bounded inert provenance for imported drafts; never executable or activation intent."""

from __future__ import annotations

import json
import re
from datetime import time
from typing import Any

from news_bulletin_playlist.models import DurationPolicyException, SourceId


def exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid portable object fields")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def strict_json(data: bytes) -> Any:
    def invalid(value: str) -> None:
        raise ValueError("Invalid JSON number")

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid)


def validate_attribution(value: object) -> dict[str, Any]:
    item = exact_object(value, {"credit", "license", "provenance", "reference"})
    for name, content in item.items():
        if content is not None and (
            not isinstance(content, str) or len(content) > 8192 or "\x00" in content
        ):
            raise ValueError("Invalid artwork attribution")
        if name == "provenance" and not isinstance(content, str):
            raise ValueError("Invalid artwork provenance")
    return item


def validate_metadata(value: str) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
        raise ValueError("Invalid imported metadata size")
    item = exact_object(
        strict_json(value.encode("utf-8")),
        {
            "original_id",
            "kind",
            "configuration",
            "cover_id",
            "cover_origin",
            "cover_sha256",
            "attribution",
        },
    )
    if (
        not isinstance(item["kind"], str)
        or not isinstance(item["cover_origin"], str)
        or item["kind"] not in {"managed", "draft"}
        or item["cover_origin"]
        not in {
            "bundled",
            "uploaded",
        }
    ):
        raise ValueError("Invalid imported provenance")
    for name in ("original_id", "cover_id"):
        if not isinstance(item[name], str) or not 0 < len(item[name]) <= 128:
            raise ValueError("Invalid imported provenance identifier")
    configuration = exact_object(item["configuration"], {"enabled", "hidden"})
    if item["kind"] == "managed":
        valid = type(configuration["enabled"]) is bool and configuration["hidden"] is None
    else:
        valid = configuration["enabled"] is None and type(configuration["hidden"]) is bool
    if not valid:
        raise ValueError("Invalid observational configuration")
    if (
        not isinstance(item["cover_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", item["cover_sha256"]) is None
    ):
        raise ValueError("Invalid original cover digest")
    validate_attribution(item["attribution"])


def exception_payload(item: DurationPolicyException) -> dict[str, object]:
    return {
        "id": item.id,
        "source_id": str(item.source_id),
        "edition_local_time": item.edition_local_time.isoformat(timespec="microseconds"),
        "edition_fold": item.edition_local_time.fold,
        "max_seconds": item.max_seconds,
    }


def parse_exception(value: object) -> DurationPolicyException:
    item = exact_object(
        value, {"id", "source_id", "edition_local_time", "edition_fold", "max_seconds"}
    )
    for name in ("id", "source_id"):
        if (
            not isinstance(item[name], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item[name]) is None
        ):
            raise ValueError("Invalid duration exception identity")
    if (
        not isinstance(item["edition_local_time"], str)
        or re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{6}", item["edition_local_time"]) is None
    ):
        raise ValueError("Invalid duration exception time")
    if (
        type(item["edition_fold"]) is not int
        or item["edition_fold"] not in (0, 1)
        or type(item["max_seconds"]) is not int
        or not 1 <= item["max_seconds"] <= 2**63 - 1
    ):
        raise ValueError("Invalid duration exception policy")
    return DurationPolicyException(
        item["id"],
        SourceId(item["source_id"]),
        time.fromisoformat(item["edition_local_time"]).replace(fold=item["edition_fold"]),
        item["max_seconds"],
    )

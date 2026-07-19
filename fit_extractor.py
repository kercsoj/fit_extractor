#!/usr/bin/env python3
"""Convert a FIT activity into two JSON files.

The output folder is named:
    YYYY-MM-DD_HH-MM-SS_<FIT filename stem>

The folder contains exactly:
    full.json      Every decoded FIT message, plus archival decoder metadata.
    analysis.json  A compact normalized schema for workout analysis.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import shutil
import statistics
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from garmin_fit_sdk import Decoder, Stream
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'garmin-fit-sdk'. Install it with:\n"
        "    python -m pip install -r requirements.txt"
    ) from exc

FIT_EPOCH_UNIX_SECONDS = 631_065_600
SEMICIRCLES_TO_DEGREES = 180.0 / (2**31)
PACKAGE_VERSION = "2.1.0"
DEFAULT_SAMPLE_INTERVAL_S = 5.0
DEFAULT_SPLIT_DISTANCE_M = 1000.0
DEFAULT_ELEVATION_SMOOTHING_WINDOW_S = 15.0


class ExtractionError(RuntimeError):
    """Raised when a FIT file cannot be extracted."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Decode a FIT activity into full.json for archival and analysis.json "
            "for normalized workout analysis."
        )
    )
    parser.add_argument("fit_file", type=Path, help="Path to the input .fit file")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Parent directory for the output folder (default: input file directory)",
    )
    parser.add_argument(
        "--timezone",
        default="auto",
        help=(
            "Timezone for local timestamps, such as Europe/Budapest or UTC. "
            "Default: auto, using the FIT local timestamp/offset when available."
        ),
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help=(
            "Optional JSON file containing context values. It may contain either "
            "the context object itself or an object with a top-level 'context' key."
        ),
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_S,
        metavar="SECONDS",
        help=f"Interval between analysis samples (default: {DEFAULT_SAMPLE_INTERVAL_S:g})",
    )
    parser.add_argument(
        "--split-distance",
        type=float,
        default=DEFAULT_SPLIT_DISTANCE_M,
        metavar="METRES",
        help=f"Automatic split length (default: {DEFAULT_SPLIT_DISTANCE_M:g})",
    )
    parser.add_argument(
        "--elevation-smoothing-window",
        type=float,
        default=DEFAULT_ELEVATION_SMOOTHING_WINDOW_S,
        metavar="SECONDS",
        help=(
            "Centered moving-average time window applied after a 5-point median filter "
            f"(default: {DEFAULT_ELEVATION_SMOOTHING_WINDOW_S:g} seconds)"
        ),
    )
    parser.add_argument(
        "--include-gps-accuracy",
        action="store_true",
        help="Add gps_accuracy_m to each analysis sample when available",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output folder with the same name",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        dest="create_zip",
        help="Also create a ZIP archive next to the output folder",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail when the Garmin decoder reports any errors",
    )
    parser.add_argument(
        "--no-crc",
        action="store_true",
        help="Disable CRC validation while decoding",
    )
    return parser.parse_args(argv)


def safe_component(value: str) -> str:
    value = value.strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return value or "fit_activity"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def as_float(value: Any) -> float | None:
    return float(value) if is_number(value) else None


def rounded(value: Any, digits: int = 3) -> float | int | None:
    if not is_number(value):
        return None
    number = round(float(value), digits)
    if digits == 0:
        return int(number)
    if number == 0:
        number = 0.0
    return number


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def fit_local_timestamp_to_naive(value: Any) -> datetime | None:
    if not is_number(value):
        return None
    try:
        return datetime.fromtimestamp(
            float(value) + FIT_EPOCH_UNIX_SECONDS, tz=timezone.utc
        ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def first_datetime(messages: Mapping[str, list[dict[str, Any]]]) -> datetime | None:
    for key, field in (
        ("session_mesgs", "start_time"),
        ("activity_mesgs", "timestamp"),
        ("record_mesgs", "timestamp"),
        ("file_id_mesgs", "time_created"),
    ):
        for row in messages.get(key, []):
            value = row.get(field)
            if isinstance(value, datetime):
                return ensure_utc(value)
    return None


def infer_fixed_timezone(
    messages: Mapping[str, list[dict[str, Any]]],
) -> timezone | None:
    for key in ("activity_mesgs", "session_mesgs"):
        for row in messages.get(key, []):
            utc_value = row.get("timestamp") or row.get("start_time")
            local_value = fit_local_timestamp_to_naive(row.get("local_timestamp"))
            if not isinstance(utc_value, datetime) or local_value is None:
                continue
            raw_offset = local_value - ensure_utc(utc_value).replace(tzinfo=None)
            if timedelta(hours=-14) <= raw_offset <= timedelta(hours=14):
                seconds = int(round(raw_offset.total_seconds() / 60.0) * 60)
                return timezone(timedelta(seconds=seconds))
    return None


def resolve_timezone(
    timezone_name: str, messages: Mapping[str, list[dict[str, Any]]]
) -> tuple[Any, str]:
    if timezone_name.lower() != "auto":
        if timezone_name.upper() == "UTC":
            return timezone.utc, "UTC"
        try:
            return ZoneInfo(timezone_name), timezone_name
        except ZoneInfoNotFoundError as exc:
            raise ExtractionError(
                f"Unknown timezone '{timezone_name}'. Use an IANA name such as "
                "Europe/Budapest, America/New_York, or UTC."
            ) from exc

    inferred = infer_fixed_timezone(messages)
    if inferred is not None:
        offset = inferred.utcoffset(None) or timedelta(0)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        return inferred, f"inferred UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return local_tz, str(local_tz)


def resolve_folder_datetime(
    messages: Mapping[str, list[dict[str, Any]]],
    local_tz: Any,
    input_path: Path,
    timezone_name: str,
) -> tuple[datetime, str]:
    if timezone_name.lower() == "auto":
        for row in messages.get("activity_mesgs", []):
            local_naive = fit_local_timestamp_to_naive(row.get("local_timestamp"))
            if local_naive is not None:
                return local_naive, "activity.local_timestamp"

    start_utc = first_datetime(messages)
    if start_utc is not None:
        return start_utc.astimezone(local_tz).replace(tzinfo=None), "activity start timestamp"

    file_mtime = datetime.fromtimestamp(input_path.stat().st_mtime, tz=local_tz)
    return file_mtime.replace(tzinfo=None), "input file modification time"


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"__type__": "bytes", "hex": value.hex()}
    if isinstance(value, bytearray):
        return {"__type__": "bytearray", "hex": bytes(value).hex()}
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "value"):
        try:
            return json_safe(value.value)
        except Exception:
            pass
    return str(value)


def write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(json_safe(value), stream, ensure_ascii=False, indent=2, sort_keys=False)
        stream.write("\n")


def decode_fit(
    input_path: Path, enable_crc_check: bool
) -> tuple[dict[str, list[dict[str, Any]]], list[Any]]:
    try:
        messages, errors = Decoder(Stream.from_file(str(input_path))).read(
            enable_crc_check=enable_crc_check
        )
    except Exception as exc:
        raise ExtractionError(f"Unable to decode '{input_path}': {exc}") from exc

    normalized: dict[str, list[dict[str, Any]]] = {}
    for key, rows in messages.items():
        normalized[str(key)] = [dict(row) for row in rows]
    return normalized, list(errors)


def first_row(messages: Mapping[str, list[dict[str, Any]]], key: str) -> dict[str, Any]:
    rows = messages.get(key, [])
    return rows[0] if rows else {}


def selected_altitude(row: Mapping[str, Any]) -> float | None:
    value = as_float(row.get("enhanced_altitude"))
    return value if value is not None else as_float(row.get("altitude"))


def selected_speed(row: Mapping[str, Any]) -> float | None:
    value = as_float(row.get("enhanced_speed"))
    return value if value is not None else as_float(row.get("speed"))


def merge_record_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge multiple FIT record messages that share the same timestamp."""
    by_timestamp: OrderedDict[datetime, dict[str, Any]] = OrderedDict()
    for row in sorted(
        records,
        key=lambda item: ensure_utc(item["timestamp"])
        if isinstance(item.get("timestamp"), datetime)
        else datetime.max.replace(tzinfo=timezone.utc),
    ):
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue
        timestamp = ensure_utc(timestamp)
        merged = by_timestamp.setdefault(timestamp, {"timestamp": timestamp})
        for key, value in row.items():
            if key != "timestamp" and value is not None:
                merged[key] = value
    return list(by_timestamp.values())


def interpolate_sparse_values(
    axes: list[float], values: list[float | None], *, carry_edges: bool = True
) -> list[float | None]:
    known = [(axis, value) for axis, value in zip(axes, values) if value is not None]
    if not known:
        return [None] * len(axes)

    known_axes = [item[0] for item in known]
    known_values = [item[1] for item in known]
    result: list[float | None] = []
    for axis in axes:
        position = bisect.bisect_left(known_axes, axis)
        if position < len(known_axes) and known_axes[position] == axis:
            result.append(known_values[position])
        elif position == 0:
            result.append(known_values[0] if carry_edges else None)
        elif position == len(known_axes):
            result.append(known_values[-1] if carry_edges else None)
        else:
            left_axis = known_axes[position - 1]
            right_axis = known_axes[position]
            left_value = known_values[position - 1]
            right_value = known_values[position]
            ratio = (axis - left_axis) / (right_axis - left_axis)
            result.append(left_value + ratio * (right_value - left_value))
    return result


def centered_filter(
    values: list[float | None], window: int, *, median: bool
) -> list[float | None]:
    if window <= 1:
        return list(values)
    if window % 2 == 0:
        window += 1
    radius = window // 2
    result: list[float | None] = []
    for index in range(len(values)):
        candidates = [
            value
            for value in values[max(0, index - radius) : min(len(values), index + radius + 1)]
            if value is not None
        ]
        if not candidates:
            result.append(None)
        elif median:
            result.append(float(statistics.median(candidates)))
        else:
            result.append(sum(candidates) / len(candidates))
    return result


def centered_time_average(
    elapsed_times: list[float], values: list[float | None], window_s: float
) -> list[float | None]:
    """Apply a centered moving average using a time window in seconds."""
    if window_s <= 0:
        raise ExtractionError("--elevation-smoothing-window must be greater than zero")
    if not elapsed_times:
        return []

    radius_s = window_s / 2.0
    result: list[float | None] = []
    for elapsed in elapsed_times:
        left = bisect.bisect_left(elapsed_times, elapsed - radius_s)
        right = bisect.bisect_right(elapsed_times, elapsed + radius_s)
        candidates = [value for value in values[left:right] if value is not None]
        result.append(sum(candidates) / len(candidates) if candidates else None)
    return result


def elevation_source(records: list[dict[str, Any]]) -> str | None:
    has_enhanced = any(is_number(row.get("enhanced_altitude")) for row in records)
    uses_fallback = any(
        not is_number(row.get("enhanced_altitude")) and is_number(row.get("altitude"))
        for row in records
    )
    has_standard = any(is_number(row.get("altitude")) for row in records)

    if has_enhanced and uses_fallback:
        return "enhanced_altitude_with_altitude_fallback"
    if has_enhanced:
        return "enhanced_altitude"
    if has_standard:
        return "altitude"
    return None


def build_analysis_points(
    records: list[dict[str, Any]], start_utc: datetime
) -> list[dict[str, Any]]:
    merged = merge_record_messages(records)
    if not merged:
        return []

    elapsed = [
        max(0.0, (ensure_utc(row["timestamp"]) - start_utc).total_seconds())
        for row in merged
    ]
    raw_distances = [as_float(row.get("distance")) for row in merged]
    distances = interpolate_sparse_values(elapsed, raw_distances, carry_edges=True)

    # Cumulative FIT distance should never decrease. Guard against malformed or
    # noisy sources so distance-based split interpolation remains stable.
    maximum_seen = 0.0
    for index, value in enumerate(distances):
        if value is None:
            continue
        maximum_seen = max(maximum_seen, value)
        distances[index] = maximum_seen

    points: list[dict[str, Any]] = []
    for index, row in enumerate(merged):
        lat = as_float(row.get("position_lat"))
        lon = as_float(row.get("position_long"))
        points.append(
            {
                "elapsed_time_s": elapsed[index],
                "distance_m": distances[index],
                "speed_m_s": selected_speed(row),
                "heart_rate_bpm": as_float(row.get("heart_rate")),
                "power_w": as_float(row.get("power")),
                "altitude_m": selected_altitude(row),
                "latitude": lat * SEMICIRCLES_TO_DEGREES if lat is not None else None,
                "longitude": lon * SEMICIRCLES_TO_DEGREES if lon is not None else None,
                "gps_accuracy_m": as_float(row.get("gps_accuracy")),
            }
        )
    return points


def apply_elevation_smoothing(points: list[dict[str, Any]], window_s: float) -> None:
    if window_s <= 0:
        raise ExtractionError("--elevation-smoothing-window must be greater than zero")
    elapsed = [float(point["elapsed_time_s"]) for point in points]
    altitudes = [as_float(point.get("altitude_m")) for point in points]
    filled = interpolate_sparse_values(elapsed, altitudes, carry_edges=True)
    median_filtered = centered_filter(filled, 5, median=True)
    smoothed = centered_time_average(elapsed, median_filtered, window_s)
    for point, altitude in zip(points, smoothed):
        point["smoothed_altitude_m"] = altitude


def crossing_at_distance(
    points: list[dict[str, Any]], target_distance_m: float
) -> dict[str, Any]:
    distances = [
        float(point["distance_m"]) if is_number(point.get("distance_m")) else 0.0
        for point in points
    ]
    index = bisect.bisect_left(distances, target_distance_m)
    if index <= 0:
        result = dict(points[0])
        result["distance_m"] = target_distance_m
        return result
    if index >= len(points):
        result = dict(points[-1])
        result["distance_m"] = target_distance_m
        return result

    left = points[index - 1]
    right = points[index]
    left_distance = float(left.get("distance_m") or 0.0)
    right_distance = float(right.get("distance_m") or left_distance)
    ratio = (
        (target_distance_m - left_distance) / (right_distance - left_distance)
        if right_distance > left_distance
        else 1.0
    )
    result: dict[str, Any] = {"distance_m": target_distance_m}
    for key in (
        "elapsed_time_s",
        "speed_m_s",
        "heart_rate_bpm",
        "power_w",
        "altitude_m",
        "smoothed_altitude_m",
        "latitude",
        "longitude",
        "gps_accuracy_m",
    ):
        left_value = as_float(left.get(key))
        right_value = as_float(right.get(key))
        if left_value is not None and right_value is not None:
            result[key] = left_value + ratio * (right_value - left_value)
        elif left_value is not None:
            result[key] = left_value
        else:
            result[key] = right_value
    return result


def interpolate_point_at_elapsed(
    points: list[dict[str, Any]], elapsed_time_s: float
) -> dict[str, Any]:
    axes = [float(point["elapsed_time_s"]) for point in points]
    index = bisect.bisect_left(axes, elapsed_time_s)
    if index <= 0:
        result = dict(points[0])
        result["elapsed_time_s"] = elapsed_time_s
        return result
    if index >= len(points):
        # A final FIT record may contain only a subset of fields (commonly final
        # distance and power). Keep the final cumulative values and carry forward
        # the latest available measurement for fields omitted by that record.
        result: dict[str, Any] = {"elapsed_time_s": elapsed_time_s}
        for key in (
            "distance_m",
            "speed_m_s",
            "heart_rate_bpm",
            "power_w",
            "altitude_m",
            "latitude",
            "longitude",
            "gps_accuracy_m",
        ):
            result[key] = next(
                (float(point[key]) for point in reversed(points) if is_number(point.get(key))),
                None,
            )
        return result

    left = points[index - 1]
    right = points[index]
    left_time = float(left["elapsed_time_s"])
    right_time = float(right["elapsed_time_s"])
    ratio = (
        (elapsed_time_s - left_time) / (right_time - left_time)
        if right_time > left_time
        else 1.0
    )
    result: dict[str, Any] = {"elapsed_time_s": elapsed_time_s}
    for key in (
        "distance_m",
        "speed_m_s",
        "heart_rate_bpm",
        "power_w",
        "altitude_m",
        "latitude",
        "longitude",
        "gps_accuracy_m",
    ):
        left_value = as_float(left.get(key))
        right_value = as_float(right.get(key))
        if left_value is not None and right_value is not None:
            result[key] = left_value + ratio * (right_value - left_value)
        elif left_value is not None:
            result[key] = left_value
        else:
            result[key] = right_value
    return result


def elevation_change(nodes: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    altitudes = [
        as_float(node.get("smoothed_altitude_m"))
        for node in nodes
        if as_float(node.get("smoothed_altitude_m")) is not None
    ]
    if len(altitudes) < 2:
        return None, None
    gain = 0.0
    loss = 0.0
    for previous, current in zip(altitudes, altitudes[1:]):
        delta = current - previous
        if delta > 0:
            gain += delta
        elif delta < 0:
            loss -= delta
    return gain, loss


def numeric_series(nodes: list[dict[str, Any]], key: str) -> list[float]:
    return [float(node[key]) for node in nodes if is_number(node.get(key))]


def segment_nodes_by_distance(
    points: list[dict[str, Any]], start_distance_m: float, end_distance_m: float
) -> list[dict[str, Any]]:
    nodes = [crossing_at_distance(points, start_distance_m)]
    nodes.extend(
        point
        for point in points
        if is_number(point.get("distance_m"))
        and start_distance_m < float(point["distance_m"]) < end_distance_m
    )
    nodes.append(crossing_at_distance(points, end_distance_m))
    return nodes


def create_splits(
    points: list[dict[str, Any]], total_distance_m: float, split_distance_m: float
) -> list[dict[str, Any]]:
    if split_distance_m <= 0:
        raise ExtractionError("--split-distance must be greater than zero")
    if not points or total_distance_m <= 0:
        return []

    split_count = int(math.ceil(total_distance_m / split_distance_m))
    splits: list[dict[str, Any]] = []
    for index in range(split_count):
        start_distance = index * split_distance_m
        end_distance = min((index + 1) * split_distance_m, total_distance_m)
        nodes = segment_nodes_by_distance(points, start_distance, end_distance)
        start_node = nodes[0]
        end_node = nodes[-1]
        duration = float(end_node.get("elapsed_time_s") or 0.0) - float(
            start_node.get("elapsed_time_s") or 0.0
        )
        distance = end_distance - start_distance
        heart_rates = numeric_series(nodes, "heart_rate_bpm")
        powers = numeric_series(nodes, "power_w")
        gain, loss = elevation_change(nodes)
        start_altitude = as_float(start_node.get("smoothed_altitude_m"))
        end_altitude = as_float(end_node.get("smoothed_altitude_m"))

        splits.append(
            OrderedDict(
                [
                    ("index", index + 1),
                    ("start_distance_m", rounded(start_distance)),
                    ("end_distance_m", rounded(end_distance)),
                    ("distance_m", rounded(distance)),
                    ("duration_s", rounded(duration)),
                    (
                        "pace_s_per_km",
                        rounded(duration / (distance / 1000.0)) if distance > 0 else None,
                    ),
                    (
                        "average_heart_rate_bpm",
                        rounded(sum(heart_rates) / len(heart_rates), 2)
                        if heart_rates
                        else None,
                    ),
                    (
                        "maximum_heart_rate_bpm",
                        rounded(max(heart_rates), 0) if heart_rates else None,
                    ),
                    (
                        "average_power_w",
                        rounded(sum(powers) / len(powers), 2) if powers else None,
                    ),
                    ("maximum_power_w", rounded(max(powers), 0) if powers else None),
                    ("start_altitude_m", rounded(start_altitude, 2)),
                    ("end_altitude_m", rounded(end_altitude, 2)),
                    ("elevation_gain_m", rounded(gain, 2)),
                    ("elevation_loss_m", rounded(loss, 2)),
                    (
                        "net_elevation_change_m",
                        rounded(end_altitude - start_altitude, 2)
                        if start_altitude is not None and end_altitude is not None
                        else None,
                    ),
                ]
            )
        )
    return splits


def create_samples(
    points: list[dict[str, Any]],
    elapsed_time_s: float,
    interval_s: float,
    include_gps_accuracy: bool,
) -> list[dict[str, Any]]:
    if interval_s <= 0:
        raise ExtractionError("--sample-interval must be greater than zero")
    if not points:
        return []

    targets: list[float] = []
    target = 0.0
    while target <= elapsed_time_s + 1e-9:
        targets.append(target)
        target += interval_s

    # Always close the series at the precise activity duration. When the session
    # duration extends a fraction of a second beyond the final FIT record, the
    # final record's measurements are retained at that exact endpoint.
    if not targets or abs(targets[-1] - elapsed_time_s) > 1e-9:
        targets.append(elapsed_time_s)

    samples: list[dict[str, Any]] = []
    for target in targets:
        point = interpolate_point_at_elapsed(points, target)
        sample = OrderedDict(
            [
                ("elapsed_time_s", rounded(target)),
                ("distance_m", rounded(point.get("distance_m"))),
                ("speed_m_s", rounded(point.get("speed_m_s"))),
                ("heart_rate_bpm", rounded(point.get("heart_rate_bpm"), 2)),
                ("power_w", rounded(point.get("power_w"), 2)),
                ("altitude_m", rounded(point.get("altitude_m"), 2)),
                ("latitude", rounded(point.get("latitude"), 7)),
                ("longitude", rounded(point.get("longitude"), 7)),
            ]
        )
        if include_gps_accuracy:
            sample["gps_accuracy_m"] = rounded(point.get("gps_accuracy_m"), 2)
        samples.append(sample)
    return samples


def event_elapsed(timestamp: Any, start_utc: datetime) -> float | None:
    if not isinstance(timestamp, datetime):
        return None
    return max(0.0, (ensure_utc(timestamp) - start_utc).total_seconds())


def create_events(
    messages: Mapping[str, list[dict[str, Any]]],
    start_utc: datetime,
    end_elapsed_s: float,
) -> list[dict[str, Any]]:
    candidates: list[tuple[float, str]] = []
    seen_start = False

    event_rows = sorted(
        messages.get("event_mesgs", []),
        key=lambda row: ensure_utc(row["timestamp"])
        if isinstance(row.get("timestamp"), datetime)
        else datetime.max.replace(tzinfo=timezone.utc),
    )
    for row in event_rows:
        elapsed = event_elapsed(row.get("timestamp"), start_utc)
        if elapsed is None:
            continue
        raw_event = str(row.get("event") or "").lower()
        raw_type = str(row.get("event_type") or "").lower()

        normalized: str | None = None
        if raw_event == "lap" or raw_type == "marker":
            normalized = "lap"
        elif raw_type == "start":
            if not seen_start:
                normalized = "start"
                seen_start = True
            elif elapsed > 2.0:
                normalized = "resume"
        elif raw_type in {
            "stop",
            "stop_all",
            "stop_disable",
            "stop_disable_all",
        }:
            normalized = "stop" if abs(elapsed - end_elapsed_s) <= 2.0 else "pause"

        if normalized is not None:
            candidates.append((elapsed, normalized))

    # Some FIT producers represent laps only in lap messages.
    for row in messages.get("lap_mesgs", []):
        if str(row.get("lap_trigger") or "").lower() == "session_end":
            continue
        elapsed = event_elapsed(row.get("timestamp"), start_utc)
        if elapsed is not None and abs(elapsed - end_elapsed_s) > 2.0:
            candidates.append((elapsed, "lap"))

    candidates.append((0.0, "start"))
    # FIT event timestamps often have whole-second precision while the session
    # duration has millisecond precision. Replace near-final stop events with one
    # canonical stop at the exact activity duration.
    candidates = [
        item
        for item in candidates
        if not (item[1] == "stop" and abs(item[0] - end_elapsed_s) <= 2.0)
    ]
    candidates.append((end_elapsed_s, "stop"))

    # Remove duplicates generated by overlapping event/lap messages. Also remove
    # a lap marker at the exact final stop, which carries no additional meaning.
    candidates.sort(key=lambda item: (item[0], {"start": 0, "pause": 1, "resume": 2, "lap": 3, "stop": 4}[item[1]]))
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for elapsed, event_type in candidates:
        if event_type == "lap" and abs(elapsed - end_elapsed_s) <= 2.0:
            continue
        dedupe_key = (int(round(elapsed * 1000)), event_type)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(
            OrderedDict(
                [
                    ("elapsed_time_s", rounded(elapsed)),
                    ("type", event_type),
                ]
            )
        )
    return result


def default_context() -> OrderedDict[str, Any]:
    return OrderedDict(
        [
            (
                "weather",
                OrderedDict(
                    [
                        ("temperature_c", None),
                        ("humidity_pct", None),
                        ("wind_speed_m_s", None),
                        ("pressure_hpa", None),
                    ]
                ),
            ),
            (
                "hydration",
                OrderedDict(
                    [
                        ("weight_before_kg", None),
                        ("weight_after_kg", None),
                        ("fluid_during_ml", None),
                        ("urine_during_ml", None),
                        ("body_mass_loss_pct", None),
                        ("estimated_sweat_loss_l", None),
                        ("sweat_rate_l_per_hour", None),
                    ]
                ),
            ),
            (
                "nutrition",
                OrderedDict(
                    [
                        ("carbohydrate_during_g", None),
                        ("caffeine_before_mg", None),
                        ("coffee_before", None),
                    ]
                ),
            ),
            (
                "electrolytes",
                OrderedDict(
                    [
                        ("sodium_during_mg", None),
                        ("potassium_during_mg", None),
                        ("magnesium_during_mg", None),
                    ]
                ),
            ),
            ("perceived_effort_1_to_10", None),
            ("notes", None),
        ]
    )


def deep_merge_known(target: dict[str, Any], source: Mapping[str, Any], path: str = "context") -> None:
    for key, value in source.items():
        if key not in target:
            raise ExtractionError(f"Unknown field in context JSON: {path}.{key}")
        if isinstance(target[key], dict):
            if not isinstance(value, Mapping):
                raise ExtractionError(f"Expected an object at {path}.{key}")
            deep_merge_known(target[key], value, f"{path}.{key}")
        else:
            target[key] = value


def load_context(context_path: Path | None, moving_time_s: float | None) -> OrderedDict[str, Any]:
    context = default_context()
    if context_path is not None:
        context_path = context_path.expanduser().resolve()
        if not context_path.is_file():
            raise ExtractionError(f"Context JSON does not exist: {context_path}")
        try:
            with context_path.open("r", encoding="utf-8") as stream:
                supplied = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ExtractionError(f"Unable to read context JSON '{context_path}': {exc}") from exc
        if not isinstance(supplied, Mapping):
            raise ExtractionError("Context JSON must contain an object")
        if "context" in supplied:
            supplied = supplied["context"]
        if not isinstance(supplied, Mapping):
            raise ExtractionError("The top-level 'context' value must be an object")
        deep_merge_known(context, supplied)

    hydration = context["hydration"]
    before = as_float(hydration.get("weight_before_kg"))
    after = as_float(hydration.get("weight_after_kg"))
    fluid_ml = as_float(hydration.get("fluid_during_ml"))
    urine_ml = as_float(hydration.get("urine_during_ml"))

    if before is not None and after is not None and before > 0:
        mass_loss_kg = before - after
        hydration["body_mass_loss_pct"] = rounded(100.0 * mass_loss_kg / before, 3)
        if fluid_ml is not None:
            estimated_sweat_loss_l = mass_loss_kg + fluid_ml / 1000.0 - (urine_ml or 0.0) / 1000.0
            hydration["estimated_sweat_loss_l"] = rounded(estimated_sweat_loss_l, 3)
            if is_number(moving_time_s) and float(moving_time_s) > 0:
                hydration["sweat_rate_l_per_hour"] = rounded(
                    estimated_sweat_loss_l / (float(moving_time_s) / 3600.0), 3
                )
    return context


def create_activity(
    input_path: Path,
    messages: Mapping[str, list[dict[str, Any]]],
    points: list[dict[str, Any]],
    local_tz: Any,
    elevation_window_s: float,
) -> tuple[OrderedDict[str, Any], datetime, float, float]:
    session = first_row(messages, "session_mesgs")
    activity_message = first_row(messages, "activity_mesgs")

    start_utc_value = session.get("start_time") or first_datetime(messages)
    if not isinstance(start_utc_value, datetime):
        raise ExtractionError("The FIT file does not contain a usable activity start time")
    start_utc = ensure_utc(start_utc_value)

    record_end = max(
        (
            ensure_utc(row["timestamp"])
            for row in messages.get("record_mesgs", [])
            if isinstance(row.get("timestamp"), datetime)
        ),
        default=None,
    )

    elapsed_time_s = as_float(session.get("total_elapsed_time"))
    if elapsed_time_s is None:
        elapsed_time_s = (
            (record_end - start_utc).total_seconds() if record_end is not None else 0.0
        )
    moving_time_s = as_float(session.get("total_timer_time"))
    if moving_time_s is None:
        moving_time_s = as_float(activity_message.get("total_timer_time"))
    if moving_time_s is None:
        moving_time_s = elapsed_time_s

    end_utc = record_end or (start_utc + timedelta(seconds=elapsed_time_s))

    total_distance_m = as_float(session.get("total_distance"))
    point_distances = numeric_series(points, "distance_m")
    if total_distance_m is None:
        total_distance_m = max(point_distances) if point_distances else 0.0

    heart_rates = numeric_series(points, "heart_rate_bpm")
    powers = numeric_series(points, "power_w")
    raw_altitudes = numeric_series(points, "altitude_m")
    gain, loss = elevation_change(points)

    pace = (
        moving_time_s / (total_distance_m / 1000.0)
        if total_distance_m > 0 and moving_time_s is not None
        else None
    )
    pause_time_s = (
        max(0.0, elapsed_time_s - moving_time_s)
        if elapsed_time_s is not None and moving_time_s is not None
        else None
    )
    strides = session.get("total_strides")
    if not is_number(strides) and str(session.get("sport") or "").lower() == "running":
        strides = session.get("total_cycles")

    elevation_calculation = OrderedDict(
        [
            ("source", elevation_source(messages.get("record_mesgs", []))),
            ("smoothing", "moving_average"),
            ("window_s", rounded(elevation_window_s)),
            ("pre_filter", "median_5_samples"),
        ]
    )

    activity = OrderedDict(
        [
            ("file_name", input_path.name),
            ("sport", session.get("sport")),
            ("start_time_local", start_utc.astimezone(local_tz).isoformat()),
            ("end_time_local", end_utc.astimezone(local_tz).isoformat()),
            ("distance_m", rounded(total_distance_m)),
            ("elapsed_time_s", rounded(elapsed_time_s)),
            ("moving_time_s", rounded(moving_time_s)),
            ("pause_time_s", rounded(pause_time_s)),
            ("average_pace_s_per_km", rounded(pace, 2)),
            (
                "average_heart_rate_bpm",
                rounded(sum(heart_rates) / len(heart_rates), 2) if heart_rates else None,
            ),
            (
                "maximum_heart_rate_bpm",
                rounded(max(heart_rates), 0) if heart_rates else None,
            ),
            (
                "average_power_w",
                rounded(sum(powers) / len(powers), 2) if powers else None,
            ),
            ("maximum_power_w", rounded(max(powers), 0) if powers else None),
            ("minimum_altitude_m", rounded(min(raw_altitudes), 2) if raw_altitudes else None),
            ("maximum_altitude_m", rounded(max(raw_altitudes), 2) if raw_altitudes else None),
            ("elevation_gain_m", rounded(gain, 2)),
            ("elevation_loss_m", rounded(loss, 2)),
            ("elevation_calculation", elevation_calculation),
            ("calories_kcal", rounded(session.get("total_calories"), 0)),
            ("strides", rounded(strides, 0)),
        ]
    )
    return activity, start_utc, elapsed_time_s, total_distance_m


def create_full_json(
    input_path: Path,
    messages: Mapping[str, list[dict[str, Any]]],
    errors: list[Any],
    enable_crc_check: bool,
    timezone_label: str,
    folder_timestamp_source: str,
) -> OrderedDict[str, Any]:
    return OrderedDict(
        [
            ("format_version", PACKAGE_VERSION),
            (
                "source",
                OrderedDict(
                    [
                        ("file_name", input_path.name),
                        ("source_path", str(input_path)),
                        ("size_bytes", input_path.stat().st_size),
                        ("sha256", sha256_file(input_path)),
                    ]
                ),
            ),
            (
                "extraction",
                OrderedDict(
                    [
                        ("generated_at_utc", datetime.now(timezone.utc)),
                        ("sdk", "garmin-fit-sdk"),
                        ("crc_check_enabled", enable_crc_check),
                        ("timezone", timezone_label),
                        ("folder_timestamp_source", folder_timestamp_source),
                        (
                            "message_counts",
                            OrderedDict((key, len(rows)) for key, rows in messages.items()),
                        ),
                        ("decoder_errors", [str(error) for error in errors]),
                    ]
                ),
            ),
            ("messages", messages),
        ]
    )


def prepare_output_folder(path: Path, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            raise ExtractionError(
                f"Output folder already exists: {path}\n"
                "Use --overwrite to replace it or choose another --output-root."
            )
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.mkdir(parents=True, exist_ok=False)


def extract_fit(
    input_path: Path,
    output_root: Path | None = None,
    timezone_name: str = "auto",
    context_path: Path | None = None,
    sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
    split_distance_m: float = DEFAULT_SPLIT_DISTANCE_M,
    elevation_smoothing_window: float = DEFAULT_ELEVATION_SMOOTHING_WINDOW_S,
    include_gps_accuracy: bool = False,
    overwrite: bool = False,
    create_zip: bool = False,
    strict: bool = False,
    enable_crc_check: bool = True,
) -> tuple[Path, Path | None]:
    input_path = input_path.expanduser().resolve()
    if not input_path.is_file():
        raise ExtractionError(f"Input file does not exist: {input_path}")
    if input_path.suffix.lower() != ".fit":
        raise ExtractionError(f"Input file must have a .fit extension: {input_path.name}")
    if sample_interval_s <= 0:
        raise ExtractionError("--sample-interval must be greater than zero")
    if split_distance_m <= 0:
        raise ExtractionError("--split-distance must be greater than zero")

    messages, errors = decode_fit(input_path, enable_crc_check=enable_crc_check)
    if strict and errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise ExtractionError(f"The FIT decoder reported errors:\n{joined}")

    local_tz, timezone_label = resolve_timezone(timezone_name, messages)
    folder_dt, folder_timestamp_source = resolve_folder_datetime(
        messages, local_tz, input_path, timezone_name
    )
    folder_name = f"{folder_dt:%Y-%m-%d_%H-%M-%S}_{safe_component(input_path.stem)}"
    root = (output_root or input_path.parent).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_dir = root / folder_name
    prepare_output_folder(output_dir, overwrite=overwrite)

    try:
        session = first_row(messages, "session_mesgs")
        start_value = session.get("start_time") or first_datetime(messages)
        if not isinstance(start_value, datetime):
            raise ExtractionError("The FIT file has no usable start timestamp")
        start_utc = ensure_utc(start_value)

        points = build_analysis_points(messages.get("record_mesgs", []), start_utc)
        if not points:
            raise ExtractionError("The FIT file contains no timestamped record messages")
        apply_elevation_smoothing(points, elevation_smoothing_window)

        activity, start_utc, elapsed_time_s, total_distance_m = create_activity(
            input_path,
            messages,
            points,
            local_tz,
            elevation_smoothing_window,
        )
        # The session summary is the canonical total. Some exporters leave a
        # slightly larger final record distance because of quantization.
        for point in points:
            if is_number(point.get("distance_m")):
                point["distance_m"] = min(float(point["distance_m"]), total_distance_m)

        splits = create_splits(points, total_distance_m, split_distance_m)
        samples = create_samples(
            points,
            elapsed_time_s,
            sample_interval_s,
            include_gps_accuracy,
        )
        events = create_events(messages, start_utc, elapsed_time_s)
        context = load_context(context_path, activity.get("moving_time_s"))

        analysis = OrderedDict(
            [
                ("activity", activity),
                ("splits", splits),
                ("samples", samples),
                ("events", events),
                ("context", context),
            ]
        )
        full = create_full_json(
            input_path,
            messages,
            errors,
            enable_crc_check,
            timezone_label,
            folder_timestamp_source,
        )

        write_json(output_dir / "full.json", full)
        write_json(output_dir / "analysis.json", analysis)
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise

    zip_path: Path | None = None
    if create_zip:
        archive_base = root / output_dir.name
        existing_zip = archive_base.with_suffix(".zip")
        if existing_zip.exists():
            if overwrite:
                existing_zip.unlink()
            else:
                raise ExtractionError(
                    f"ZIP archive already exists: {existing_zip}\nUse --overwrite to replace it."
                )
        zip_path = Path(
            shutil.make_archive(
                str(archive_base), "zip", root_dir=root, base_dir=output_dir.name
            )
        )

    return output_dir, zip_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output_dir, zip_path = extract_fit(
            input_path=args.fit_file,
            output_root=args.output_root,
            timezone_name=args.timezone,
            context_path=args.context,
            sample_interval_s=args.sample_interval,
            split_distance_m=args.split_distance,
            elevation_smoothing_window=args.elevation_smoothing_window,
            include_gps_accuracy=args.include_gps_accuracy,
            overwrite=args.overwrite,
            create_zip=args.create_zip,
            strict=args.strict,
            enable_crc_check=not args.no_crc,
        )
    except ExtractionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Extracted FIT package: {output_dir}")
    print(f"  {output_dir / 'full.json'}")
    print(f"  {output_dir / 'analysis.json'}")
    if zip_path is not None:
        print(f"ZIP archive: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

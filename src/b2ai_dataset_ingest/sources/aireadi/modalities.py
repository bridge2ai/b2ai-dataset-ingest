"""The non-clinical AI-READI datatypes: file links, and device-derived measurements.

A full release ships eight datatype directories beside ``clinical_data/`` (retinal imaging,
ECG, CGM, wearable, environment), each with a ``manifest.tsv`` and then
``<modality>/<device>/<participant_id>/`` trees holding DICOM, WFDB, mHealth JSON or ESDS
CSV. Three things come out of them, all driven by ``config/aireadi/modalities/*.yaml``:

- **File links.** One ``FileReference`` per manifest file path (or per participant folder),
  carrying datatype / modality / device / format and whatever manifest columns the config
  names as attributes. The packet links the file; it never embeds it.
- **Manifest measurements.** The ECG manifest carries the device's numeric intervals (rate,
  PR, QRS, QT, QTc, axes) per recording; each configured column becomes a Measurement.
- **Series summaries.** A CGM or wearable JSON holds a 10-day time series. The packet-size
  rule (docs/plans/0002, step 20) says a series never enters a packet as samples, so the
  config names a handful of window statistics (mean, CV, time in LOINC-defined ranges,
  GMI) and each becomes one Measurement over the wear period.

Everything is read from the manifests, never by walking the tree, and every per-participant
JSON is opened one at a time. Logging names a datatype, a column or a count -- never an id,
a timestamp or a value.
"""

from __future__ import annotations

import csv
import json
import logging
import statistics
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from b2ai_dataset_ingest.mapping.loaders import is_placeholder
from b2ai_dataset_ingest.mapping.omop import AgeAnchor, as_number, iso_date
from b2ai_dataset_ingest.model import (
    FileReference,
    MeasurementObservation,
    OntologyTerm,
    Quantity,
    TimePoint,
)
from b2ai_dataset_ingest.reporting import IngestReport

logger = logging.getLogger(__name__)

#: ``--files`` modes: nothing, one link per participant folder, one link per file.
FILE_MODES = ("none", "folder", "file")

#: File-format labels by extension, for ``File.file_attributes.fileFormat``.
FORMATS = {
    ".dcm": "DICOM",
    ".hea": "WFDB",
    ".dat": "WFDB",
    ".json": "mHealth-JSON",
    ".csv": "ESDS-CSV",
}

#: Glucose management indicator (Bergenstal et al., Diabetes Care 2018): GMI % =
#: 3.31 + 0.02392 x mean glucose (mg/dL).
GMI_INTERCEPT, GMI_SLOPE = 3.31, 0.02392


def read_manifest(path: Path) -> Iterator[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh, delimiter="\t")


def load_modality_configs(config_dir: Path, loader: Callable[[Path], dict]) -> list[dict]:
    return [loader(p) for p in sorted((config_dir / "modalities").glob("*.yaml"))]


# -- file links ----------------------------------------------------------------------------
def file_references(
    root: Path,
    mapping: dict[str, Any],
    mode: str,
    report: IngestReport,
) -> dict[str, list[FileReference]]:
    """``person_id -> [FileReference]`` from one datatype's manifest.

    ``mode`` is ``file`` (one per manifest path) or ``folder`` (one per participant per
    modality/device directory, with a ``fileCount``). ``none`` yields nothing.
    """
    out: dict[str, list[FileReference]] = {}
    if mode == "none":
        return out
    datatype = mapping["datatype"]
    manifest = root / datatype / mapping.get("manifest", "manifest.tsv")
    if not manifest.exists():
        return out
    id_column = mapping.get("id_column", "participant_id")
    file_columns = list(mapping.get("file_columns") or [])
    attribute_columns = list(mapping.get("attribute_columns") or [])
    declared_format = mapping.get("file_format")
    folders: dict[tuple[str, str], tuple[FileReference, int]] = {}
    for row in read_manifest(manifest):
        person_id = (row.get(id_column) or "").strip()
        if not person_id:
            continue
        for column in file_columns:
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            uri = raw.lstrip("/")
            parts = uri.split("/")
            modality = parts[1] if len(parts) > 2 else ""
            device = parts[2] if len(parts) > 3 else ""
            attributes = {
                "datatype": datatype,
                "modality": modality,
                "device": device,
                "fileFormat": declared_format or FORMATS.get(Path(uri).suffix.lower(), ""),
            }
            for name in attribute_columns:
                value = (row.get(name) or "").strip()
                if value:
                    attributes[name] = value
            if mode == "file":
                out.setdefault(person_id, []).append(
                    FileReference(uri=uri, attributes=attributes, individual_file_id=person_id)
                )
                report.files_linked += 1
            else:
                folder = "/".join(parts[:-1])
                key = (person_id, folder)
                if key not in folders:
                    kept = {k: v for k, v in attributes.items() if k not in attribute_columns}
                    reference = FileReference(
                        uri=folder + "/", attributes=kept, individual_file_id=person_id
                    )
                    folders[key] = (reference, 0)
                reference, n = folders[key]
                folders[key] = (reference, n + 1)
    for (person_id, _), (reference, n) in folders.items():
        reference.attributes["fileCount"] = str(n)
        out.setdefault(person_id, []).append(reference)
        report.files_linked += 1
    return out


# -- measurements from the manifest (ECG intervals) --------------------------------------
def manifest_measurements(
    root: Path,
    mapping: dict[str, Any],
    anchors: dict[str, AgeAnchor],
    report: IngestReport,
) -> Iterator[tuple[str, MeasurementObservation, str]]:
    """Yield ``(person_id, Measurement, item)`` for each configured manifest column.

    One manifest row is one recording; a participant with two recordings gets two of each
    interval, told apart by the recording id in ``description``. The manifest carries no
    date, so the time is the participant's enrolment age.
    """
    datatype = mapping["datatype"]
    table = mapping.get("table", datatype)
    manifest = root / datatype / mapping.get("manifest", "manifest.tsv")
    if not manifest.exists():
        return
    id_column = mapping.get("id_column", "participant_id")
    measures: dict[str, dict] = mapping.get("measures") or {}
    recording_column = mapping.get("recording_column")
    for row in read_manifest(manifest):
        person_id = (row.get(id_column) or "").strip()
        if not person_id:
            continue
        recording = Path((row.get(recording_column) or "").strip()).stem if recording_column else ""
        anchor = anchors.get(person_id)
        when = TimePoint(session_id="enrollment", age_iso8601=anchor.iso) if anchor else None
        for item, spec in measures.items():
            raw = (row.get(item) or "").strip()
            if not raw:
                continue
            if is_placeholder(spec.get("assay")):
                report.note_placeholder_skipped(table, item)
                continue
            value = as_number(raw)
            if value is None:
                report.note_unresolved_item(table, item)
                continue
            unit = spec.get("unit")
            if not isinstance(unit, dict) or is_placeholder(unit):
                report.note_unit_unmapped(table, item)
                continue
            description = spec.get("description") or (
                f"{mapping.get('description', datatype)} recording {recording}".strip()
            )
            yield (
                person_id,
                MeasurementObservation(
                    assay=OntologyTerm(**spec["assay"]),
                    value_quantity=Quantity(value=value, unit=OntologyTerm(**unit)),
                    time=when,
                    description=description,
                ),
                item,
            )


# -- window summaries over a time series (CGM, wearable) ----------------------------------
def series_summaries(
    root: Path,
    mapping: dict[str, Any],
    anchors: dict[str, AgeAnchor],
    report: IngestReport,
) -> Iterator[tuple[str, MeasurementObservation, str]]:
    """Yield ``(person_id, Measurement, item)`` for each configured statistic of a series.

    The series lives in one mHealth JSON per participant, named by a manifest column; the
    config names where the samples are (``series.key``, ``value_path``, ``time_path``) and
    which statistics to emit. Non-numeric samples (Dexcom ``"Low"``/``"High"`` clamps) are
    counted and skipped. A participant with fewer than ``min_readings`` samples gets no
    summary, counted rather than emitted on a sliver of data.
    """
    datatype = mapping["datatype"]
    table = mapping.get("table", datatype)
    manifest = root / datatype / mapping.get("manifest", "manifest.tsv")
    if not manifest.exists():
        return
    id_column = mapping.get("id_column", "participant_id")
    series = mapping.get("series") or {}
    file_column = series.get("file_column")
    if not file_column:
        return
    min_readings = int(series.get("min_readings") or 1)
    measures: dict[str, dict] = mapping.get("measures") or {}
    for row in read_manifest(manifest):
        person_id = (row.get(id_column) or "").strip()
        raw_path = (row.get(file_column) or "").strip()
        if not person_id or not raw_path:
            continue
        path = root / raw_path.lstrip("/")
        if not path.exists():
            report.note_item_unmapped(table, f"{file_column}:missing")
            continue
        values, stamps, skipped = _load_series(path, series)
        if skipped:
            report.note_sentinel_answer(table, series.get("name", "series"))
            report.sentinel_answers[f"{table}.{series.get('name', 'series')}"] += skipped - 1
        if len(values) < min_readings:
            report.note_unresolved_item(table, f"{series.get('name', 'series')}:too_few_readings")
            continue
        first = min(stamps) if stamps else None
        days = _span_days(stamps)
        anchor = anchors.get(person_id)
        age = anchor.age_at(first[:10]) if anchor and first else (anchor.iso if anchor else None)
        when = TimePoint(session_id="wear-window", age_iso8601=age) if age else None
        window = f"{len(values)} readings over {days} day(s)" if days else f"{len(values)} readings"
        for item, spec in measures.items():
            if is_placeholder(spec.get("assay")):
                report.note_placeholder_skipped(table, item)
                continue
            value = _statistic(values, spec)
            if value is None:
                continue
            unit = spec.get("unit")
            if not isinstance(unit, dict) or is_placeholder(unit):
                report.note_unit_unmapped(table, item)
                continue
            yield (
                person_id,
                MeasurementObservation(
                    assay=OntologyTerm(**spec["assay"]),
                    value_quantity=Quantity(value=round(value, 2), unit=OntologyTerm(**unit)),
                    time=when,
                    description=f"{spec.get('description') or item}; {window}",
                ),
                item,
            )


def _load_series(path: Path, series: dict[str, Any]) -> tuple[list[float], list[str], int]:
    """``(numeric samples, timestamps, non-numeric sample count)`` from one mHealth file."""
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    node: Any = payload
    for key in series.get("key") or []:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return [], [], 0
    if not isinstance(node, list):
        return [], [], 0
    values: list[float] = []
    stamps: list[str] = []
    skipped = 0
    value_path = series.get("value_path") or []
    time_path = series.get("time_path") or []
    for sample in node:
        value = as_number(_dig(sample, value_path))
        if value is None:
            skipped += 1
            continue
        values.append(value)
        stamp = _dig(sample, time_path)
        if isinstance(stamp, str) and stamp:
            stamps.append(stamp)
    return values, stamps, skipped


def _dig(node: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _span_days(stamps: list[str]) -> int | None:
    dates = [d for d in (iso_date(s[:10]) for s in stamps) if d is not None]
    if not dates:
        return None
    return (max(dates) - min(dates)).days + 1


def _statistic(values: list[float], spec: dict[str, Any]) -> float | None:
    """One window statistic, as named by the config's ``statistic``."""
    kind = spec.get("statistic", "mean")
    if not values:
        return None
    if kind == "mean":
        return statistics.fmean(values)
    if kind == "min":
        return min(values)
    if kind == "max":
        return max(values)
    if kind == "count":
        return float(len(values))
    if kind == "cv":
        if len(values) < 2:
            return None
        mean = statistics.fmean(values)
        return 100.0 * statistics.stdev(values) / mean if mean else None
    if kind == "gmi":
        return GMI_INTERCEPT + GMI_SLOPE * statistics.fmean(values)
    if kind == "percent_in_range":
        low, high = spec.get("low"), spec.get("high")
        hits = sum(
            1
            for v in values
            if (low is None or v >= float(low)) and (high is None or v <= float(high))
        )
        return 100.0 * hits / len(values)
    logger.warning("unknown statistic %r; skipping", kind)
    return None


__all__ = [
    "FILE_MODES",
    "file_references",
    "load_modality_configs",
    "manifest_measurements",
    "series_summaries",
]

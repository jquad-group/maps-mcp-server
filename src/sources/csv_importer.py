"""CSV / JSON importer for curated POI datasets.

Use cases:
- Manually curate a list of hotels (e.g. from a one-shot Overpass Turbo
  export) and ship it as a checked-in CSV.
- Supplement Overpass results with hotels that OSM is missing.
- Import a commercial POI dataset exported as CSV.

Supported CSV columns (case-insensitive headers, only ``name,lat,lon`` are
required; the rest are optional and populate the same fields as Overpass)::

    name,lat,lon,street,housenumber,postcode,city,country,phone,website,brand

A JSON file is accepted as a single list of objects with the same keys.

Example::

    name,lat,lon,city,country
    Best Western Premier Alpen Resort Resort,47.2629,11.3946,Innsbruck,Austria
    Best Western Hotel Seefeld,47.3296,11.1894,Seefeld,Austria
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from src.services.poi_store import POI, make_place_id, now_iso
from src.sources.overpass_client import OverpassPOI

logger = logging.getLogger(__name__)

# Canonical column names (lowercased). Input headers are matched
# case-insensitively against these.
_REQUIRED = ("name", "lat", "lon")
_OPTIONAL = (
    "street", "housenumber", "postcode", "city", "country",
    "phone", "website", "brand", "stars", "source_url",
)
_ALL_COLS = _REQUIRED + _OPTIONAL

# Canonical key set used for JSON import too.
_JSON_KEYS = set(_ALL_COLS)


class ImportError_(ValueError):
    """Raised when a CSV/JSON file is malformed or missing required fields."""


def _canonical_header(raw: list[str]) -> list[str]:
    """Map raw CSV headers to canonical names (lowercase, stripped)."""
    out = []
    for h in raw:
        out.append(h.strip().lower())
    return out


def _row_to_poi(row: dict[str, str], collection: str, source_file: str) -> POI | None:
    """Convert one normalized CSV/JSON row into a :class:`POI`, or None."""
    name = (row.get("name") or "").strip()
    if not name:
        return None
    try:
        lat = float(row.get("lat", ""))
        lon = float(row.get("lon", ""))
    except (ValueError, TypeError):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    source_url = (row.get("source_url") or "").strip()
    # Prefer source_url for the stable id; fall back to name+coords so the
    # same hotel re-imported from different files doesn't dedup-splice.
    id_basis = source_url or f"{name}|{lat:.5f},{lon:.5f}"

    return POI(
        place_id=make_place_id(id_basis),
        name=name,
        street=(row.get("street") or "").strip(),
        zip=(row.get("postcode") or "").strip(),
        city=(row.get("city") or "").strip(),
        country=(row.get("country") or "").strip(),
        lat=lat,
        lon=lon,
        source_url=source_url,
        source_file=source_file,
        collection=collection,
        ingested_at=now_iso(),
        extra={
            k: row[k]
            for k in ("brand", "stars", "phone", "website", "housenumber")
            if row.get(k)
        },
    )


def import_csv(
    path: str | Path,
    *,
    collection: str = "bestwestern-de",
    delimiter: str = ",",
) -> list[POI]:
    """Import a CSV file into a list of :class:`POI`.

    Args:
        path: CSV file path.
        collection: Collection name to stamp on each POI.
        delimiter: Field delimiter (default ``,``; ``\\t`` for TSV).

    Returns:
        List of parsed POIs (rows with missing name/lat/lon are skipped with
        a warning).

    Raises:
        ImportError_: if the file is empty or missing required headers.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")  # utf-8-sig tolerates BOM
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise ImportError_(f"CSV file {p} is empty")
    headers = _canonical_header(list(reader.fieldnames))
    missing = [c for c in _REQUIRED if c not in headers]
    if missing:
        raise ImportError_(
            f"CSV {p} missing required columns: {missing}. "
            f"Found: {headers}. Required: {list(_REQUIRED)}."
        )

    pois: list[POI] = []
    skipped = 0
    for raw_row in reader:
        # Normalize keys to canonical lowercase.
        row = {(k.strip().lower() if k else ""): (v or "") for k, v in raw_row.items()}
        poi = _row_to_poi(row, collection=collection, source_file=str(p))
        if poi is None:
            skipped += 1
            continue
        pois.append(poi)
    logger.info("CSV import %s: %d POIs, %d rows skipped", p.name, len(pois), skipped)
    return pois


def import_json(
    path: str | Path,
    *,
    collection: str = "bestwestern-de",
) -> list[POI]:
    """Import a JSON file (a list of objects) into a list of :class:`POI`.

    Each object must have at least ``name``, ``lat``, ``lon``. Unknown keys
    are ignored.
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ImportError_(f"JSON {p} must be a list of objects, got {type(data).__name__}")
    pois: list[POI] = []
    skipped = 0
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            skipped += 1
            continue
        row = {k.lower(): ("" if v is None else str(v)) for k, v in item.items() if k}
        if not all(row.get(c) for c in _REQUIRED):
            skipped += 1
            continue
        poi = _row_to_poi(row, collection=collection, source_file=str(p))
        if poi is None:
            skipped += 1
            continue
        pois.append(poi)
    logger.info("JSON import %s: %d POIs, %d skipped", p.name, len(pois), skipped)
    return pois


def import_file(
    path: str | Path,
    *,
    collection: str = "bestwestern-de",
) -> list[POI]:
    """Dispatch on file extension: ``.csv``/``.tsv`` -> CSV, ``.json`` -> JSON."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return import_csv(p, collection=collection)
    if suffix == ".tsv":
        return import_csv(p, collection=collection, delimiter="\t")
    if suffix == ".json":
        return import_json(p, collection=collection)
    raise ImportError_(
        f"Unsupported file type {p.suffix!r}; use .csv, .tsv, or .json"
    )


def overpass_to_poi(op: OverpassPOI, *, collection: str) -> POI:
    """Convert an :class:`OverpassPOI` into a cacheable :class:`POI`.

    The OSM element id is the stable key, so re-fetching Overpass updates
    records in place rather than duplicating them.
    """
    extra: dict[str, Any] = {"osm_id": op.osm_id, "osm_type": op.osm_type}
    if op.brand:
        extra["brand"] = op.brand
    if op.stars:
        extra["stars"] = op.stars
    if op.phone:
        extra["phone"] = op.phone
    if op.website:
        extra["website"] = op.website
    return POI(
        place_id=make_place_id(op.osm_id),
        name=op.name,
        street=op.street,
        zip=op.postcode,
        city=op.city,
        country=op.country,
        lat=op.lat,
        lon=op.lon,
        source_url=op.website or f"osm://{op.osm_id}",
        source_file="overpass",
        collection=collection,
        ingested_at=now_iso(),
        extra=extra,
    )

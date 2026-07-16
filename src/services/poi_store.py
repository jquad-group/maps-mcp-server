"""JSON-backed cache of geocoded POIs (points of interest).

A "collection" is a named JSON file under ``POI_DATA_PATH`` (e.g.
``bestwestern-de.json``) holding a list of :class:`POI` records. The store
is the single home for everything the maps server has geocoded: hotels
ingested from crawled markdown, ad-hoc POIs supplied to ``find_within``,
etc.

The on-disk format is a plain list of dicts so it is diff-friendly and
inspectable. ``place_id`` is the stable key; re-ingesting the same source
updates records in place (idempotent).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class POI:
    """A single geocoded point of interest.

    Attributes:
        place_id: Stable identifier (e.g. sha1 of the source URL). Used as
            the dedup key.
        name: Display name.
        street: Street line (may include house number / ranges).
        zip: Postal code (PLZ).
        city: City / town.
        country: Country name.
        lat, lon: WGS-84 coordinates.
        source_url: Where this POI came from (crawl URL, etc.).
        source_file: File the record was parsed from (for traceability).
        collection: Collection this POI belongs to.
        ingested_at: ISO-8601 UTC timestamp of ingestion.
        extra: Free-form metadata bag (e.g. hotel chain, brand, phone).
    """

    place_id: str
    name: str
    street: str
    zip: str
    city: str
    country: str
    lat: float
    lon: float
    source_url: str = ""
    source_file: str = ""
    collection: str = ""
    ingested_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def address(self) -> str:
        """Single-line postal address suitable for geocoding."""
        parts = [self.street, f"{self.zip} {self.city}".strip(), self.country]
        return ", ".join(p for p in parts if p)

    @property
    def coord(self) -> tuple[float, float]:
        return (self.lat, self.lon)


class POIStore:
    """Filesystem-backed collection of POIs, one JSON file per collection.

    The store is intentionally simple: load-all / save-all. POI counts are
    small (hundreds at most for a hotel chain extract), so we don't need an
    index.
    """

    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, collection: str) -> Path:
        # Guard against path traversal on the collection name.
        safe = "".join(c for c in collection if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError(f"invalid collection name: {collection!r}")
        return self.base_dir / f"{safe}.json"

    def load(self, collection: str) -> list[POI]:
        """Load all POIs in a collection (empty list if none yet)."""
        path = self._path(collection)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("failed to read %s: %s — treating as empty", path, exc)
            return []
        out: list[POI] = []
        for item in raw:
            try:
                out.append(POI(**item))
            except TypeError:
                # Forward-compatibility: skip records with unknown fields.
                out.append(POI(**{k: v for k, v in item.items() if k in POI.__dataclass_fields__}))  # type: ignore[arg-type]
        return out

    def upsert_many(self, collection: str, pois: list[POI]) -> int:
        """Insert or update ``pois`` by ``place_id``. Returns count written."""
        existing = {p.place_id: p for p in self.load(collection)}
        for p in pois:
            p.collection = collection
            existing[p.place_id] = p
        ordered = sorted(existing.values(), key=lambda p: p.name.lower())
        self._save(collection, ordered)
        return len(pois)

    def _save(self, collection: str, pois: list[POI]) -> None:
        path = self._path(collection)
        path.write_text(
            json.dumps([p.to_dict() for p in pois], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete(self, collection: str) -> bool:
        """Delete the collection's file. Returns True if a file was removed."""
        path = self._path(collection)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear(self, collection: str) -> None:
        """Remove all POIs from a collection, leaving an empty collection.

        Unlike :meth:`delete` (which removes the file), this keeps the
        collection present on disk as an empty list — so a subsequent
        ``upsert_many`` repopulates it cleanly. Used by ``ingest_poi`` when
        ``replace=True`` to avoid accumulating stale POIs from a previous
        ingest with a different area.
        """
        self._save(collection, [])


def make_place_id(source_url: str) -> str:
    """Deterministic place_id from a source URL (sha1)."""
    return hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()

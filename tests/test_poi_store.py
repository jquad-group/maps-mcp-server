"""Tests for the JSON-backed POI store."""

from pathlib import Path

import pytest

from src.services.poi_store import POI, POIStore, make_place_id


@pytest.fixture()
def store(tmp_path: Path) -> POIStore:
    return POIStore(tmp_path)


def _poi(place_id: str, name: str, lat: float = 0.0, lon: float = 0.0) -> POI:
    return POI(
        place_id=place_id,
        name=name,
        street="Test St. 1",
        zip="00000",
        city="Testcity",
        country="Deutschland",
        lat=lat,
        lon=lon,
    )


class TestPOI:
    def test_address_property(self):
        p = _poi("1", "Hotel", lat=1.0, lon=2.0)
        assert p.address == "Test St. 1, 00000 Testcity, Deutschland"

    def test_address_strips_empty_parts(self):
        p = POI(place_id="1", name="X", street="", zip="", city="Berlin",
                country="Deutschland", lat=0, lon=0)
        assert p.address == "Berlin, Deutschland"

    def test_coord_tuple(self):
        p = _poi("1", "X", lat=49.0, lon=11.0)
        assert p.coord == (49.0, 11.0)

    def test_to_dict_roundtrip(self):
        p = _poi("1", "X", lat=1.0, lon=2.0)
        d = p.to_dict()
        assert d["place_id"] == "1"
        assert d["lat"] == 1.0
        assert d["lon"] == 2.0


class TestPOIStore:
    def test_load_empty_collection(self, store: POIStore):
        assert store.load("nope") == []

    def test_upsert_and_load(self, store: POIStore):
        store.upsert_many("hotels", [_poi("a", "Alpha"), _poi("b", "Beta")])
        loaded = store.load("hotels")
        assert {p.place_id for p in loaded} == {"a", "b"}

    def test_upsert_dedupes_by_place_id(self, store: POIStore):
        store.upsert_many("hotels", [_poi("a", "Alpha")])
        # Same id, different name -> update, not insert.
        store.upsert_many("hotels", [_poi("a", "Alpha Updated")])
        loaded = store.load("hotels")
        assert len(loaded) == 1
        assert loaded[0].name == "Alpha Updated"

    def test_upsert_sorts_by_name(self, store: POIStore):
        store.upsert_many("hotels", [_poi("z", "Zeta"), _poi("a", "Alpha")])
        loaded = store.load("hotels")
        assert [p.name for p in loaded] == ["Alpha", "Zeta"]

    def test_upsert_assigns_collection(self, store: POIStore):
        store.upsert_many("custom", [_poi("a", "Alpha")])
        assert store.load("custom")[0].collection == "custom"

    def test_delete(self, store: POIStore):
        store.upsert_many("hotels", [_poi("a", "Alpha")])
        assert store.delete("hotels") is True
        assert store.load("hotels") == []
        # Deleting again returns False.
        assert store.delete("hotels") is False

    def test_collection_name_sanitized_no_traversal(self, store: POIStore):
        # Non-alphanumeric chars are stripped, so a traversal attempt cannot
        # escape the base directory: "../escape" -> "..escape", which resolves
        # inside base_dir.
        path = store._path("../escape")  # noqa: SLF001 — test-only
        assert path.parent == store.base_dir
        assert ".." not in path.parts[-1] or path.parts[-1].startswith("..")  # noqa: E713

    def test_collection_name_only_alnum_dash_underscore(self, store: POIStore, tmp_path: Path):
        # Whatever the input, the resulting filename contains only safe chars.
        for raw in ["../escape", "good-name", "bad name!", "a/b/c", "clean_1"]:
            path = store._path(raw)  # noqa: SLF001 — test-only
            assert path.parent == store.base_dir
            # Filename must be relative to base_dir (no separators in the name).
            assert path.name.endswith(".json")

    def test_empty_collection_name_raises(self, store: POIStore):
        with pytest.raises(ValueError):
            store.load("")
        with pytest.raises(ValueError):
            store.load("///")  # all-stripped -> empty

    def test_creates_base_dir(self, tmp_path: Path):
        target = tmp_path / "nested" / "deeper"
        s = POIStore(target)
        assert target.exists()
        s.upsert_many("x", [_poi("a", "Alpha")])
        assert (target / "x.json").exists()

    def test_make_place_id_stable(self):
        a = make_place_id("https://example.com/a")
        b = make_place_id("https://example.com/a")
        assert a == b
        assert len(a) == 16

    def test_make_place_id_different_urls(self):
        assert make_place_id("u1") != make_place_id("u2")

    def test_load_corrupt_json_returns_empty(self, store: POIStore, tmp_path: Path):
        path = store._path("corrupt")  # noqa: SLF001 — test-only
        path.write_text("{not json", encoding="utf-8")
        assert store.load("corrupt") == []

    def test_persisted_json_is_utf8(self, store: POIStore):
        p = POI(place_id="1", name="Café Üni", street="Straße 1", zip="10115",
                city="Berlin", country="Deutschland", lat=0, lon=0)
        store.upsert_many("utf8", [p])
        raw = store._path("utf8").read_text(encoding="utf-8")  # noqa: SLF001
        assert "Café Üni" in raw
        assert "Straße 1" in raw

"""Tests for the CSV/JSON POI importer and the overpass_to_poi converter."""

import json
from pathlib import Path

import pytest

from src.services.poi_store import POI
from src.sources.csv_importer import (
    ImportError_,
    import_csv,
    import_file,
    import_json,
    overpass_to_poi,
)
from src.sources.overpass_client import OverpassPOI


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestImportCSV:
    def test_basic_import(self, tmp_path: Path):
        p = _write(tmp_path / "hotels.csv",
                   "name,lat,lon,city,country\n"
                   "Hotel A,51.0,6.0,Hilden,DE\n"
                   "Hotel B,49.45,11.08,Nuremberg,DE\n")
        pois = import_csv(p, collection="bw")
        assert len(pois) == 2
        assert pois[0].name == "Hotel A"
        assert pois[0].lat == pytest.approx(51.0)
        assert pois[0].city == "Hilden"
        assert pois[0].country == "DE"
        assert pois[0].collection == "bw"

    def test_case_insensitive_headers(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv",
                   "Name,LAT,Lon,City\nA,1.0,2.0,Berlin\n")
        pois = import_csv(p)
        assert len(pois) == 1
        assert pois[0].name == "A"

    def test_full_column_set(self, tmp_path: Path):
        cols = "name,lat,lon,street,housenumber,postcode,city,country,phone,website,brand,stars,source_url"
        p = _write(tmp_path / "full.csv",
                   f"{cols}\n"
                   "Hotel,50.0,7.0,Main St,1,10115,Berlin,DE,"
                   "+49 30 123,https://h.de,Best Western,4,https://h.de\n")
        pois = import_csv(p, collection="bw")
        assert len(pois) == 1
        poi = pois[0]
        assert poi.street == "Main St"
        assert poi.zip == "10115"
        assert poi.source_url == "https://h.de"
        assert poi.extra.get("brand") == "Best Western"
        assert poi.extra.get("stars") == "4"
        assert poi.extra.get("phone") == "+49 30 123"

    def test_bom_tolerated(self, tmp_path: Path):
        p = tmp_path / "bom.csv"
        p.write_bytes(b"\xef\xbb\xbfname,lat,lon\nA,1.0,2.0\n")
        pois = import_csv(p)
        assert len(pois) == 1

    def test_tsv_delimiter(self, tmp_path: Path):
        p = _write(tmp_path / "x.tsv", "name\tlat\tlon\nA\t1.0\t2.0\n")
        pois = import_csv(p, delimiter="\t")
        assert len(pois) == 1

    def test_skips_rows_missing_name(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv",
                   "name,lat,lon\nA,1,2\n,3,4\nB,5,6\n")
        pois = import_csv(p)
        assert len(pois) == 2
        assert {x.name for x in pois} == {"A", "B"}

    def test_skips_rows_with_bad_coords(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv",
                   "name,lat,lon\nA,notanumber,2\nB,200,5\nC,1,2\n")
        pois = import_csv(p)
        assert len(pois) == 1
        assert pois[0].name == "C"

    def test_skips_out_of_range_coords(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv",
                   "name,lat,lon\nA,91,0\nB,0,181\nC,50,10\n")
        pois = import_csv(p)
        assert len(pois) == 1

    def test_missing_required_columns_raises(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv", "name,lat\nA,1\n")
        with pytest.raises(ImportError_, match="missing required columns"):
            import_csv(p)

    def test_empty_file_raises(self, tmp_path: Path):
        p = _write(tmp_path / "empty.csv", "")
        with pytest.raises(ImportError_, match="empty"):
            import_csv(p)

    def test_idempotent_place_id(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv",
                   "name,lat,lon,source_url\nA,1,2,https://x/a\n")
        pois1 = import_csv(p)
        pois2 = import_csv(p)
        assert pois1[0].place_id == pois2[0].place_id


# ---------------------------------------------------------------------------
# JSON import
# ---------------------------------------------------------------------------


class TestImportJSON:
    def test_basic_import(self, tmp_path: Path):
        p = tmp_path / "hotels.json"
        p.write_text(json.dumps([
            {"name": "Hotel A", "lat": 51.0, "lon": 6.0, "city": "Hilden"},
            {"name": "Hotel B", "lat": 49.45, "lon": 11.08, "city": "Nuremberg"},
        ]), encoding="utf-8")
        pois = import_json(p, collection="bw")
        assert len(pois) == 2
        assert pois[0].name == "Hotel A"

    def test_non_list_raises(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"name": "X", "lat": 0, "lon": 0}), encoding="utf-8")
        with pytest.raises(ImportError_, match="must be a list"):
            import_json(p)

    def test_skips_items_missing_required(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps([
            {"name": "A", "lat": 1, "lon": 2},
            {"name": "B"},            # missing lat/lon
            {"lat": 1, "lon": 2},     # missing name
            {"name": "C", "lat": 3, "lon": 4},
        ]), encoding="utf-8")
        pois = import_json(p)
        assert {x.name for x in pois} == {"A", "C"}

    def test_null_values_handled(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps([
            {"name": "A", "lat": 1, "lon": 2, "city": None},
        ]), encoding="utf-8")
        pois = import_json(p)
        assert len(pois) == 1
        assert pois[0].city == ""

    def test_unknown_keys_ignored(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps([
            {"name": "A", "lat": 1, "lon": 2, "banana": "yes"},
        ]), encoding="utf-8")
        pois = import_json(p)
        assert len(pois) == 1


# ---------------------------------------------------------------------------
# import_file dispatcher
# ---------------------------------------------------------------------------


class TestImportFile:
    def test_csv_dispatch(self, tmp_path: Path):
        p = _write(tmp_path / "x.csv", "name,lat,lon\nA,1,2\n")
        pois = import_file(p)
        assert len(pois) == 1

    def test_json_dispatch(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps([{"name": "A", "lat": 1, "lon": 2}]),
                     encoding="utf-8")
        pois = import_file(p)
        assert len(pois) == 1

    def test_tsv_dispatch(self, tmp_path: Path):
        p = _write(tmp_path / "x.tsv", "name\tlat\tlon\nA\t1\t2\n")
        pois = import_file(p)
        assert len(pois) == 1

    def test_unsupported_extension_raises(self, tmp_path: Path):
        p = _write(tmp_path / "x.txt", "name,lat,lon\nA,1,2\n")
        with pytest.raises(ImportError_, match="Unsupported file type"):
            import_file(p)


# ---------------------------------------------------------------------------
# overpass_to_poi converter
# ---------------------------------------------------------------------------


class TestOverpassToPOI:
    def test_basic_conversion(self):
        op = OverpassPOI(
            osm_id="node:12345",
            osm_type="node",
            name="Best Western Hilden",
            lat=51.1681,
            lon=6.9307,
            street="Schwanenstr.",
            postcode="40721",
            city="Hilden",
            country="DE",
            phone="+49 2103 5030",
            website="https://h.de",
            brand="Best Western",
            stars="4",
        )
        poi = overpass_to_poi(op, collection="bw")
        assert isinstance(poi, POI)
        assert poi.name == "Best Western Hilden"
        assert poi.lat == pytest.approx(51.1681)
        assert poi.street == "Schwanenstr."
        assert poi.zip == "40721"
        assert poi.collection == "bw"
        assert poi.extra.get("osm_id") == "node:12345"
        assert poi.extra.get("brand") == "Best Western"
        assert poi.extra.get("stars") == "4"

    def test_stable_id_from_osm_id(self):
        op = OverpassPOI(osm_id="node:1", osm_type="node", name="X",
                         lat=0, lon=0)
        a = overpass_to_poi(op, collection="bw")
        b = overpass_to_poi(op, collection="bw")
        assert a.place_id == b.place_id

    def test_source_url_falls_back_to_osm(self):
        op = OverpassPOI(osm_id="node:1", osm_type="node", name="X",
                         lat=0, lon=0, website="")
        poi = overpass_to_poi(op, collection="bw")
        assert poi.source_url == "osm://node:1"

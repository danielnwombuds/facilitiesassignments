#!/usr/bin/env python3
"""Download a Google Sheet as CSV and write facilities.geojson for the map."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_SHEET_NAME = "DSHS Data Raw"

# Map spreadsheet headers (case-insensitive) to GeoJSON property keys.
HEADER_ALIASES: dict[str, str] = {
    "facility_name": "name",
    "facility_type": "type",
    "facility_subtype": "subtype",
    "address": "address",
    "county": "county",
    "assignee": "assignee",
    "color": "color",
    "reports_location": "reports_location",
    "service_disclosure": "service_disclosure",
    "bed_count": "beds",
    "specialty": "specialties",
    "facility_poc": "facility_poc",
    "coordinates": "coordinates",
    "latitude": "latitude",
    "longitude": "longitude",
    "street_address": "street_address",
    "city": "city",
    "state": "state",
    "zip_code": "zip_code",
}


def normalize_header(header: str) -> str:
    return re.sub(r"\s+", "_", header.strip().lower())


def sheet_csv_url(sheet_id: str, sheet_name: str, gid: str | None) -> str:
    if gid:
        return (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
            f"?format=csv&gid={urllib.parse.quote(gid)}"
        )
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    )


def fetch_csv(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "dshs-facility-map/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Failed to download sheet ({exc.code}). "
            "Confirm the spreadsheet is shared as 'Anyone with the link can view' "
            "and that GOOGLE_SHEET_ID / SHEET_NAME (or SHEET_GID) are correct."
        ) from exc


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_coordinates(row: dict[str, str]) -> tuple[float, float] | None:
    coords = (row.get("coordinates") or "").strip()
    if coords:
        parts = [part.strip() for part in coords.split(",")]
        if len(parts) >= 2:
            lat = parse_float(parts[0])
            lng = parse_float(parts[1])
            if lat is not None and lng is not None:
                return lat, lng

    lat = parse_float(row.get("latitude"))
    lng = parse_float(row.get("longitude"))
    if lat is not None and lng is not None:
        return lat, lng
    return None


def build_address(row: dict[str, str]) -> str:
    address = (row.get("address") or "").strip()
    if address:
        return address

    parts = [
        (row.get("street_address") or "").strip(),
        (row.get("city") or "").strip(),
        (row.get("state") or "").strip(),
        (row.get("zip_code") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def row_to_feature(row: dict[str, str]) -> dict[str, Any] | None:
    coords = parse_coordinates(row)
    if coords is None:
        return None

    lat, lng = coords
    name = (row.get("name") or "").strip()
    if not name:
        return None

    properties = {
        "name": name,
        "type": (row.get("type") or "").strip(),
        "subtype": (row.get("subtype") or "").strip(),
        "address": build_address(row),
        "county": (row.get("county") or "").strip(),
        "assignee": (row.get("assignee") or "Unassigned").strip() or "Unassigned",
        "color": (row.get("color") or "Grey").strip() or "Grey",
        "reports_location": (row.get("reports_location") or "").strip(),
        "service_disclosure": (row.get("service_disclosure") or "").strip(),
        "beds": (row.get("beds") or "").strip(),
        "specialties": (row.get("specialties") or "").strip(),
        "facility_poc": (row.get("facility_poc") or "").strip(),
    }

    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lng, lat],
        },
        "properties": properties,
    }


def csv_to_geojson(csv_text: str) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise SystemExit("Sheet CSV has no header row.")

    header_map: dict[str, str] = {}
    for header in reader.fieldnames:
        if header is None:
            continue
        key = HEADER_ALIASES.get(normalize_header(header))
        if key:
            header_map[header] = key

    features: list[dict[str, Any]] = []
    skipped = 0
    for raw_row in reader:
        row = {
            header_map[source]: (raw_row.get(source) or "")
            for source in header_map
        }
        feature = row_to_feature(row)
        if feature is None:
            skipped += 1
            continue
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "feature_count": len(features),
            "skipped_rows": skipped,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet-id", required=True, help="Google Spreadsheet ID")
    parser.add_argument(
        "--sheet-name",
        default=DEFAULT_SHEET_NAME,
        help=f'Sheet tab name (default: "{DEFAULT_SHEET_NAME}")',
    )
    parser.add_argument(
        "--gid",
        default="",
        help="Optional sheet gid (preferred when the tab name has spaces)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/data/facilities.geojson"),
        help="Output GeoJSON path",
    )
    args = parser.parse_args()

    url = sheet_csv_url(args.sheet_id, args.sheet_name, args.gid or None)
    print(f"Downloading sheet from: {url}", file=sys.stderr)
    csv_text = fetch_csv(url)
    geojson = csv_to_geojson(csv_text)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(geojson, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    meta = geojson["metadata"]
    print(
        f"Wrote {meta['feature_count']} features "
        f"({meta['skipped_rows']} rows skipped) to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

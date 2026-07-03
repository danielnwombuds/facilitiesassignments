#!/usr/bin/env python3
"""
Compare Mon Ami nursing home CSV against WA DSHS ArcGIS GeoJSON data.

Matching: license ID first (normalized), then fuzzy name+address match.
Outputs a readable CSV diff report.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.request
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ARC_GIS_URL = (
    "https://services2.arcgis.com/WW3T8U6q5EkZ9U3n/arcgis/rest/services/"
    "Long_Term_Care_Nursing_Homes_view/FeatureServer/1/query"
    "?outFields=*&where=1%3D1&f=geojson"
)

# Mon Ami CSV column -> (JSON property, normalizer key).
# district_label is Mon Ami-specific (service areas) and is not compared.
# note is ignored per requirements.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "name": ("nf_name", "text"),
    "license ID": ("nf_license_num", "license"),
    "address_line1": ("nf_loc_street_address", "address"),
    "address_line2": ("acAddress2", "address"),
    "city": ("nf_loc_city", "text"),
    "state": ("acState", "text"),
    "zip": ("nf_loc_zip_cde", "zip"),
    "bed_count": ("TOTAL_Beds_nf_bed_count", "bed"),  # see get_json_bed_count()
}

# DSHS bed fields in GeoJSON (only TOTAL is used for diffing):
#   TOTAL_Beds_nf_bed_count  — licensed bed capacity (matches DSHS rate reports)
#   T1819_Beds_nf_bed_count  — Medicare/Medicaid (Title 18/19) certified beds, often lower
#   XVIII_Beds_nf_bed_count  — Medicare-certified beds only
#   XIX_Beds_nf_bed_count    — Medicaid-certified beds only
JSON_BED_FIELDS: dict[str, str] = {
    "TOTAL_Beds_nf_bed_count": "TOTAL",
    "T1819_Beds_nf_bed_count": "T18/19",
    "XVIII_Beds_nf_bed_count": "XVIII",
    "XIX_Beds_nf_bed_count": "XIX",
}

ORDINAL_WORDS = {
    "FIRST": "1ST",
    "SECOND": "2ND",
    "THIRD": "3RD",
    "FOURTH": "4TH",
    "FIFTH": "5TH",
    "SIXTH": "6TH",
    "SEVENTH": "7TH",
    "EIGHTH": "8TH",
    "NINTH": "9TH",
    "TENTH": "10TH",
    "ELEVENTH": "11TH",
    "TWELFTH": "12TH",
}

# Applied longest-first; maps address tokens to a canonical form for comparison.
ADDRESS_REPLACEMENTS = (
    (r"\bNORTHEAST\b", "NORTHEAST"),
    (r"\bNORTHWEST\b", "NORTHWEST"),
    (r"\bSOUTHEAST\b", "SOUTHEAST"),
    (r"\bSOUTHWEST\b", "SOUTHWEST"),
    (r"\bNE\b", "NORTHEAST"),
    (r"\bNW\b", "NORTHWEST"),
    (r"\bSE\b", "SOUTHEAST"),
    (r"\bSW\b", "SOUTHWEST"),
    (r"\bNORTH\b", "NORTH"),
    (r"\bSOUTH\b", "SOUTH"),
    (r"\bEAST\b", "EAST"),
    (r"\bWEST\b", "WEST"),
    (r"\bN\b", "NORTH"),
    (r"\bS\b", "SOUTH"),
    (r"\bE\b", "EAST"),
    (r"\bW\b", "WEST"),
    (r"\bBOULEVARD\b", "BOULEVARD"),
    (r"\bBLVD\b", "BOULEVARD"),
    (r"\bPARKWAY\b", "PARKWAY"),
    (r"\bPKWY\b", "PARKWAY"),
    (r"\bHIGHWAY\b", "HIGHWAY"),
    (r"\bHWY\b", "HIGHWAY"),
    (r"\bSTREET\b", "STREET"),
    (r"\bST\b", "STREET"),
    (r"\bAVENUE\b", "AVENUE"),
    (r"\bAVE\b", "AVENUE"),
    (r"\bAV\b", "AVENUE"),
    (r"\bROAD\b", "ROAD"),
    (r"\bRD\b", "ROAD"),
    (r"\bDRIVE\b", "DRIVE"),
    (r"\bDR\b", "DRIVE"),
    (r"\bLANE\b", "LANE"),
    (r"\bLN\b", "LANE"),
    (r"\bCOURT\b", "COURT"),
    (r"\bCT\b", "COURT"),
    (r"\bPLACE\b", "PLACE"),
    (r"\bPL\b", "PLACE"),
    (r"\bCIRCLE\b", "CIRCLE"),
    (r"\bCIR\b", "CIRCLE"),
    (r"\bTERRACE\b", "TERRACE"),
    (r"\bTER\b", "TERRACE"),
    (r"\bTRAIL\b", "TRAIL"),
    (r"\bTRL\b", "TRAIL"),
    (r"\bWAY\b", "WAY"),
    (r"\bSQUARE\b", "SQUARE"),
    (r"\bSQ\b", "SQUARE"),
    (r"\bMOUNT\b", "MOUNT"),
    (r"\bMT\b", "MOUNT"),
    (r"\bBUILDING\b", "BUILDING"),
    (r"\bBLDG\b", "BUILDING"),
    (r"\bSUITE\b", "SUITE"),
    (r"\bSTE\b", "SUITE"),
    (r"\bAPARTMENT\b", "APARTMENT"),
    (r"\bAPT\b", "APARTMENT"),
    (r"\bFLOOR\b", "FLOOR"),
    (r"\bFL\b", "FLOOR"),
)

STREET_TYPE_TOKENS = frozenset(
    {
        "STREET",
        "AVENUE",
        "ROAD",
        "DRIVE",
        "LANE",
        "COURT",
        "PLACE",
        "BOULEVARD",
        "PARKWAY",
        "HIGHWAY",
        "CIRCLE",
        "TERRACE",
        "TRAIL",
        "WAY",
        "SQUARE",
    }
)

# Weights for composite fuzzy score (name + address).
FUZZY_NAME_WEIGHT = 0.5
FUZZY_ADDRESS_WEIGHT = 0.5
FUZZY_ZIP_MATCH_BONUS = 0.05
LICENSE_MATCH_MIN_ADDRESS = 0.60
SUCCESSOR_MIN_ADDRESS = 0.92
DUPLICATE_MIN_ADDRESS = 0.85
FED_PROVIDER_MIN_ADDRESS = 0.85

OUTPUT_COLUMNS = [
    "status",
    "match_method",
    "mon_ami_id",
    "monami_name",
    "dshs_name",
    "license_id_monami",
    "license_id_dshs",
    "field",
    "monami_value",
    "dshs_value",
    "gdl_archive_date",
    "fuzzy_score",
    "notes",
]


def normalize_license(value: Any) -> str:
    if value is None or value == "":
        return ""
    s = str(value).strip().upper()
    s = re.sub(r"^NH-?", "", s)
    s = s.lstrip("0") or "0"
    return s


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_zip(value: Any) -> str:
    if value is None or value == "":
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits[:5] if digits else ""


def normalize_bed(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return normalize_text(value)


def normalize_county(value: Any) -> str:
    s = normalize_text(value)
    s = re.sub(r"\s+county\s*$", "", s, flags=re.IGNORECASE)
    return s


def normalize_ordinals(value: str) -> str:
    s = value
    for word, replacement in ORDINAL_WORDS.items():
        s = re.sub(rf"\b{word}\b", replacement, s)
    s = re.sub(r"\b(\d+)\s+(ST|ND|RD|TH)\b", r"\1\2", s)

    def add_ordinal_suffix(match: re.Match[str]) -> str:
        num = int(match.group(1))
        street_type = match.group(2)
        if 11 <= num % 100 <= 13:
            suffix = "TH"
        else:
            suffix = {1: "ST", 2: "ND", 3: "RD"}.get(num % 10, "TH")
        return f"{num}{suffix} {street_type}"

    s = re.sub(
        r"\b(\d{1,2})\s+(AVENUE|STREET|ROAD|DRIVE|LANE|COURT|PLACE|WAY|CIRCLE)\b",
        add_ordinal_suffix,
        s,
    )
    return s


def merge_compound_directions(value: str) -> str:
    s = value
    for pair, combined in (
        (r"\bNORTH\s+EAST\b", "NORTHEAST"),
        (r"\bNORTH\s+WEST\b", "NORTHWEST"),
        (r"\bSOUTH\s+EAST\b", "SOUTHEAST"),
        (r"\bSOUTH\s+WEST\b", "SOUTHWEST"),
    ):
        s = re.sub(pair, combined, s)
    return s


def normalize_address_for_match(value: Any) -> str:
    s = normalize_text(value).upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = normalize_ordinals(s)
    for pattern, replacement in ADDRESS_REPLACEMENTS:
        s = re.sub(pattern, replacement, s)
    s = merge_compound_directions(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_address(value: Any) -> str:
    return normalize_address_for_match(value)


def addresses_equal(monami_val: Any, json_val: Any) -> bool:
    a = normalize_address(monami_val)
    b = normalize_address(json_val)
    if not a and not b:
        return True
    if a == b:
        return True
    if frozenset(a.split()) == frozenset(b.split()):
        return True
    for longer, shorter in ((a, b), (b, a)):
        if not shorter:
            continue
        if longer.startswith(shorter + " "):
            suffix = longer[len(shorter) + 1 :]
            if suffix in STREET_TYPE_TOKENS:
                return True
    return False


NORMALIZERS = {
    "text": normalize_text,
    "address": normalize_address,
    "license": normalize_license,
    "zip": normalize_zip,
    "bed": normalize_bed,
    "county": normalize_county,
}


def values_equal(monami_val: Any, json_val: Any, norm_key: str) -> bool:
    if norm_key == "address":
        return addresses_equal(monami_val, json_val)
    norm = NORMALIZERS[norm_key]
    a = norm(monami_val)
    b = norm(json_val)
    if norm_key in ("text", "county"):
        return a.casefold() == b.casefold()
    return a == b


def display_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_json_bed_count(props: dict[str, Any]) -> Any:
    """Licensed bed count for comparison. TOTAL matches DSHS rate-report capacity."""
    total = props.get("TOTAL_Beds_nf_bed_count")
    if total is not None and str(total).strip() != "":
        return total
    return props.get("T1819_Beds_nf_bed_count", "")


def format_json_address(props: dict[str, Any]) -> str:
    """Human-readable address from DSHS GeoJSON properties."""
    street = display_value(props.get("nf_loc_street_address"))
    city = display_value(props.get("nf_loc_city"))
    state = display_value(props.get("acState")) or "WA"
    zip_code = normalize_zip(props.get("nf_loc_zip_cde"))
    parts = [p for p in (street, city, f"{state} {zip_code}".strip()) if p]
    return ", ".join(parts)


def format_monami_address(row: dict[str, str]) -> str:
    """Human-readable address from a Mon Ami CSV row."""
    street = display_value(row.get("address_line1"))
    line2 = display_value(row.get("address_line2"))
    city = display_value(row.get("city"))
    state = display_value(row.get("state")) or "WA"
    zip_code = normalize_zip(row.get("zip"))
    street_part = ", ".join(p for p in (street, line2) if p)
    parts = [p for p in (street_part, city, f"{state} {zip_code}".strip()) if p]
    return ", ".join(parts)


def format_bed_count_context(props: dict[str, Any]) -> str:
    """Summarize all DSHS bed fields when reporting a bed_count diff."""
    parts = []
    for field, label in JSON_BED_FIELDS.items():
        val = props.get(field)
        if val is not None and str(val).strip() != "":
            parts.append(f"{label}={display_value(val)}")
    compared = normalize_bed(get_json_bed_count(props))
    return (
        f"Compared using licensed TOTAL ({compared}). "
        f"DSHS values: {', '.join(parts) or 'none'}. "
        f"T18/19/XVIII/XIX are certified-bed subsets, not licensed totals."
    )


def text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a).casefold(), normalize_text(b).casefold()).ratio()


def monami_location_key(row: dict[str, str]) -> str:
    parts = [
        row.get("address_line1", ""),
        row.get("address_line2", ""),
        row.get("city", ""),
        normalize_zip(row.get("zip", "")),
    ]
    return " ".join(normalize_address_for_match(p) for p in parts if normalize_text(p))


def json_location_key(props: dict[str, Any]) -> str:
    parts = [
        props.get("nf_loc_street_address", ""),
        props.get("acAddress2", ""),
        props.get("nf_loc_city", ""),
        normalize_zip(props.get("nf_loc_zip_cde", "")),
    ]
    return " ".join(normalize_address_for_match(p) for p in parts if normalize_text(p))


def composite_similarity(
    monami_row: dict[str, str], json_props: dict[str, Any]
) -> tuple[float, float, float]:
    """Return composite, name, and address similarity scores."""
    name_score = text_similarity(monami_row.get("name", ""), json_props.get("nf_name", ""))
    address_score = text_similarity(
        monami_location_key(monami_row), json_location_key(json_props)
    )
    composite = (
        FUZZY_NAME_WEIGHT * name_score + FUZZY_ADDRESS_WEIGHT * address_score
    )
    monami_zip = normalize_zip(monami_row.get("zip", ""))
    json_zip = normalize_zip(json_props.get("nf_loc_zip_cde", ""))
    if monami_zip and json_zip and monami_zip == json_zip:
        composite = min(1.0, composite + FUZZY_ZIP_MATCH_BONUS)
    return composite, name_score, address_score


def format_fuzzy_score(composite: float, name_score: float, address_score: float) -> str:
    return f"{composite:.2f} (n={name_score:.2f},a={address_score:.2f})"


def passes_fuzzy_match(
    composite: float, name_score: float, address_score: float, threshold: float
) -> bool:
    """Accept matches via name-led, address-led (CHOW/rename), or balanced paths."""
    # Same location, renamed operator (CHOW) — lean on address.
    if address_score >= 0.88 and name_score >= 0.25:
        return True
    if composite < threshold - 0.05:
        return False
    # Same name, possibly new license and/or moved address.
    if name_score >= 0.72 and composite >= threshold:
        return True
    # Both name and address reasonably close.
    if (
        name_score >= 0.65
        and address_score >= 0.80
        and composite >= threshold
    ):
        return True
    return False


def load_geojson(source: Path | None, fetch: bool) -> dict[str, Any]:
    if fetch:
        print(f"Fetching latest data from ArcGIS...", file=sys.stderr)
        with urllib.request.urlopen(ARC_GIS_URL, timeout=120) as resp:
            return json.load(resp)
    path = source or Path("query.json")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_monami(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # Handle BOM-prefixed first column name if present.
    for row in rows:
        for key in list(row.keys()):
            if key.lstrip("\ufeff") == "Mon Ami ID" and key != "Mon Ami ID":
                row["Mon Ami ID"] = row.pop(key)
    return rows


def is_archived(props: dict[str, Any]) -> bool:
    return bool(display_value(props.get("GDLArchiveDate")))


def pick_best_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer current (non-archived) rows; ArcGIS returns full license history."""
    active = [r for r in records if not is_archived(r)]
    pool = active if active else records
    return max(pool, key=lambda r: display_value(r.get("GDLPublishDate")) or "0")


def build_json_indexes(
    features: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Return all props, best record per license, and active-only record per license."""
    all_json: list[dict[str, Any]] = []
    license_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for feature in features:
        props = feature.get("properties", {})
        all_json.append(props)
        lic = normalize_license(props.get("nf_license_num"))
        if lic:
            license_groups[lic].append(props)

    json_by_license = {
        lic: pick_best_record(records) for lic, records in license_groups.items()
    }
    active_by_license = {
        lic: pick_best_record([r for r in records if not is_archived(r)])
        for lic, records in license_groups.items()
        if any(not is_archived(r) for r in records)
    }
    fed_provider_by_num = {
        normalize_license(props.get("nf_fed_provider_num")): props
        for props in active_by_license.values()
        if normalize_license(props.get("nf_fed_provider_num"))
    }
    return all_json, json_by_license, active_by_license, fed_provider_by_num


def json_record_key(props: dict[str, Any]) -> str:
    return str(props.get("OBJECTID", props.get("nf_license_num", "")))


def available_fuzzy_candidates(
    active_by_license: dict[str, dict[str, Any]],
    matched_json_licenses: set[str],
    json_by_license: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer unmatched active licenses; fall back to any unmatched license."""
    active = [
        props
        for lic, props in active_by_license.items()
        if lic not in matched_json_licenses
    ]
    if active:
        return active
    return [
        props
        for lic, props in json_by_license.items()
        if lic not in matched_json_licenses
    ]


def compare_fields(
    monami_row: dict[str, str], json_props: dict[str, Any]
) -> list[tuple[str, str, str]]:
    diffs: list[tuple[str, str, str]] = []
    for csv_field, (json_field, norm_key) in FIELD_MAP.items():
        monami_val = monami_row.get(csv_field, "")
        if csv_field == "bed_count":
            json_val = get_json_bed_count(json_props)
        else:
            json_val = json_props.get(json_field, "")
        if not values_equal(monami_val, json_val, norm_key):
            diffs.append((csv_field, display_value(monami_val), display_value(json_val)))
    return diffs


def find_fuzzy_match(
    monami_row: dict[str, str],
    candidates: list[dict[str, Any]],
    threshold: float,
) -> tuple[dict[str, Any] | None, float, float, float]:
    best_composite = 0.0
    best_name = 0.0
    best_address = 0.0
    best_props: dict[str, Any] | None = None

    for props in candidates:
        composite, name_score, address_score = composite_similarity(monami_row, props)
        if composite > best_composite:
            best_composite = composite
            best_name = name_score
            best_address = address_score
            best_props = props

    if best_props is not None and passes_fuzzy_match(
        best_composite, best_name, best_address, threshold
    ):
        return best_props, best_composite, best_name, best_address
    return None, best_composite, best_name, best_address


def find_active_successor(
    prior_props: dict[str, Any],
    active_by_license: dict[str, dict[str, Any]],
    matched_json_licenses: set[str],
) -> dict[str, Any] | None:
    """Find the current active license at the same location after a CHOW/re-license."""
    prior_lic = normalize_license(prior_props.get("nf_license_num"))
    prior_addr = json_location_key(prior_props)
    best_props: dict[str, Any] | None = None
    best_addr = 0.0

    for lic, props in active_by_license.items():
        if lic in matched_json_licenses or lic == prior_lic:
            continue
        addr_score = text_similarity(prior_addr, json_location_key(props))
        if addr_score >= SUCCESSOR_MIN_ADDRESS and addr_score > best_addr:
            best_addr = addr_score
            best_props = props
    return best_props


def monami_street_zip_key(row: dict[str, str]) -> str:
    return " ".join(
        [
            normalize_address_for_match(row.get("address_line1", "")),
            normalize_zip(row.get("zip", "")),
        ]
    ).strip()


def json_street_zip_key(props: dict[str, Any]) -> str:
    return " ".join(
        [
            normalize_address_for_match(props.get("nf_loc_street_address", "")),
            normalize_zip(props.get("nf_loc_zip_cde", "")),
        ]
    ).strip()


def find_duplicate_match(
    monami_row: dict[str, str],
    active_by_license: dict[str, dict[str, Any]],
    matched_json_licenses: set[str],
    license_to_monami: dict[str, list[str]],
) -> tuple[dict[str, Any] | None, str, float, float, float]:
    """If DSHS record already matched, link when same location (duplicate Mon Ami row)."""
    best_props: dict[str, Any] | None = None
    best_scores = (0.0, 0.0, 0.0)
    best_lic = ""

    for lic, props in active_by_license.items():
        if lic not in matched_json_licenses:
            continue
        composite, name_score, address_score = composite_similarity(monami_row, props)
        street_zip_score = text_similarity(
            monami_street_zip_key(monami_row), json_street_zip_key(props)
        )
        location_score = max(address_score, street_zip_score)
        if location_score >= DUPLICATE_MIN_ADDRESS and composite > best_scores[0]:
            best_scores = (composite, name_score, location_score)
            best_props = props
            best_lic = lic

    if best_props is None:
        return None, "", 0.0, 0.0, 0.0

    owners = license_to_monami.get(best_lic, [])
    owner_note = ", ".join(owners) if owners else best_lic
    return best_props, owner_note, *best_scores


def run_diff(
    monami_rows: list[dict[str, str]],
    geojson: dict[str, Any],
    fuzzy_threshold: float,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    features = geojson.get("features", [])
    all_json, json_by_license, active_by_license, fed_provider_by_num = build_json_indexes(
        features
    )

    monami_licenses = {
        normalize_license(row.get("license ID", ""))
        for row in monami_rows
        if normalize_license(row.get("license ID", ""))
    }

    matched_json_keys: set[str] = set()
    matched_json_licenses: set[str] = set()
    license_to_monami: dict[str, list[str]] = defaultdict(list)
    output_rows: list[dict[str, str]] = []
    counts = {
        "matched_by_license": 0,
        "matched_by_fuzzy": 0,
        "license_renewed": 0,
        "archived": 0,
        "field_diffs": 0,
        "duplicate_monami": 0,
        "unmatched_monami": 0,
        "new_records": 0,
        "unchanged": 0,
    }

    def add_row(**kwargs: str) -> None:
        row = {col: "" for col in OUTPUT_COLUMNS}
        row.update(kwargs)
        output_rows.append(row)

    # Match Mon Ami records against DSHS source.
    for monami_row in monami_rows:
        mon_ami_id = monami_row.get("Mon Ami ID", "")
        monami_name = monami_row.get("name", "")
        monami_license = monami_row.get("license ID", "")
        json_props: dict[str, Any] | None = None
        match_method = ""
        fuzzy_score = ""

        lic_key = normalize_license(monami_license)
        if lic_key and lic_key in json_by_license:
            candidate = json_by_license[lic_key]
            _, _, addr_score = composite_similarity(monami_row, candidate)
            if addr_score >= LICENSE_MATCH_MIN_ADDRESS:
                json_props = candidate
                match_method = "license_id"
                counts["matched_by_license"] += 1

        if json_props is None and lic_key and lic_key in fed_provider_by_num:
            candidate = fed_provider_by_num[lic_key]
            _, _, addr_score = composite_similarity(monami_row, candidate)
            if addr_score >= FED_PROVIDER_MIN_ADDRESS:
                json_props = candidate
                match_method = "fed_provider_num"
                counts["matched_by_license"] += 1

        if json_props is None:
            available = available_fuzzy_candidates(
                active_by_license, matched_json_licenses, json_by_license
            )
            json_props, composite, name_score, address_score = find_fuzzy_match(
                monami_row, available, fuzzy_threshold
            )
            if json_props is not None:
                match_method = "fuzzy_name_address"
                fuzzy_score = format_fuzzy_score(composite, name_score, address_score)
                counts["matched_by_fuzzy"] += 1
            else:
                dup_props, owner_note, composite, name_score, address_score = (
                    find_duplicate_match(
                        monami_row,
                        active_by_license,
                        matched_json_licenses,
                        license_to_monami,
                    )
                )
                if dup_props is not None:
                    add_row(
                        status="DUPLICATE_MONAMI",
                        match_method="same_location",
                        mon_ami_id=mon_ami_id,
                        monami_name=monami_name,
                        dshs_name=display_value(dup_props.get("nf_name")),
                        license_id_monami=monami_license,
                        license_id_dshs=display_value(dup_props.get("nf_license_num")),
                        fuzzy_score=format_fuzzy_score(composite, name_score, address_score),
                        notes=(
                            f"Duplicate Mon Ami row — DSHS license "
                            f"{display_value(dup_props.get('nf_license_num'))} already matched to "
                            f"Mon Ami ID(s) {owner_note}. Archive or merge this entry."
                        ),
                    )
                    counts["duplicate_monami"] += 1
                    continue

                add_row(
                    status="UNMATCHED_IN_DSHS",
                    match_method="none",
                    mon_ami_id=mon_ami_id,
                    monami_name=monami_name,
                    license_id_monami=monami_license,
                    fuzzy_score=format_fuzzy_score(composite, name_score, address_score)
                    if composite > 0
                    else "",
                    notes="In Mon Ami but not found in DSHS data (by license or name+address).",
                )
                counts["unmatched_monami"] += 1
                continue

        jlic = normalize_license(json_props.get("nf_license_num"))
        license_to_monami[jlic].append(mon_ami_id)
        matched_json_keys.add(json_record_key(json_props))
        matched_json_licenses.add(normalize_license(json_props.get("nf_license_num")))

        diff_props = json_props
        archive_date = display_value(json_props.get("GDLArchiveDate"))
        dshs_name = display_value(json_props.get("nf_name"))
        dshs_license = display_value(json_props.get("nf_license_num"))

        if archive_date:
            successor = find_active_successor(
                json_props, active_by_license, matched_json_licenses
            )
            if successor is not None:
                succ_lic = normalize_license(successor.get("nf_license_num"))
                matched_json_licenses.add(succ_lic)
                matched_json_keys.add(json_record_key(successor))
                diff_props = successor
                add_row(
                    status="LICENSE_RENEWED",
                    match_method=match_method,
                    mon_ami_id=mon_ami_id,
                    monami_name=monami_name,
                    dshs_name=display_value(successor.get("nf_name")),
                    license_id_monami=monami_license,
                    license_id_dshs=display_value(successor.get("nf_license_num")),
                    gdl_archive_date=archive_date,
                    fuzzy_score=fuzzy_score,
                    notes=(
                        f"Old license {dshs_license} ({dshs_name}) is archived in DSHS. "
                        f"Active replacement is license "
                        f"{display_value(successor.get('nf_license_num'))} "
                        f"({display_value(successor.get('nf_name'))}). "
                        f"Mon Ami address: {format_monami_address(monami_row)}. "
                        f"Old DSHS address: {format_json_address(json_props)}. "
                        f"New DSHS address: {format_json_address(successor)}. "
                        f"Update Mon Ami — do not add as a new facility."
                    ),
                )
                counts["license_renewed"] += 1
                dshs_name = display_value(successor.get("nf_name"))
                dshs_license = display_value(successor.get("nf_license_num"))
                archive_date = ""
            else:
                add_row(
                    status="ARCHIVED",
                    match_method=match_method,
                    mon_ami_id=mon_ami_id,
                    monami_name=monami_name,
                    dshs_name=dshs_name,
                    license_id_monami=monami_license,
                    license_id_dshs=dshs_license,
                    gdl_archive_date=archive_date,
                    fuzzy_score=fuzzy_score,
                    notes="Facility has GDLArchiveDate in DSHS data; consider archiving in Mon Ami.",
                )
                counts["archived"] += 1

        diffs = compare_fields(monami_row, diff_props)
        if diffs:
            for field, monami_val, dshs_val in diffs:
                row_notes = ""
                if field == "bed_count":
                    row_notes = format_bed_count_context(diff_props)
                add_row(
                    status="FIELD_DIFF",
                    match_method=match_method,
                    mon_ami_id=mon_ami_id,
                    monami_name=monami_name,
                    dshs_name=dshs_name,
                    license_id_monami=monami_license,
                    license_id_dshs=dshs_license,
                    field=field,
                    monami_value=monami_val,
                    dshs_value=dshs_val,
                    gdl_archive_date=archive_date,
                    fuzzy_score=fuzzy_score,
                    notes=row_notes,
                )
                counts["field_diffs"] += 1
        elif not archive_date:
            add_row(
                status="OK",
                match_method=match_method,
                mon_ami_id=mon_ami_id,
                monami_name=monami_name,
                dshs_name=dshs_name,
                license_id_monami=monami_license,
                license_id_dshs=dshs_license,
                fuzzy_score=fuzzy_score,
            )
            counts["unchanged"] += 1

    # Active DSHS licenses not represented in Mon Ami (one row per license).
    new_record_licenses: set[str] = set()
    for lic, props in sorted(active_by_license.items()):
        if lic in monami_licenses or lic in matched_json_licenses:
            continue
        if lic in new_record_licenses:
            continue
        new_record_licenses.add(lic)

        city = display_value(props.get("nf_loc_city"))
        zip_code = normalize_zip(props.get("nf_loc_zip_cde"))
        beds = display_value(get_json_bed_count(props))
        county = display_value(props.get("NF_County_Name"))
        address = display_value(props.get("nf_loc_street_address"))
        add_row(
            status="NEW_RECORD",
            match_method="none",
            dshs_name=display_value(props.get("nf_name")),
            license_id_dshs=display_value(props.get("nf_license_num")),
            field="(new facility)",
            dshs_value=address,
            notes=(
                f"Add to Mon Ami | address: {address} | city: {city} | "
                f"zip: {zip_code} | beds: {beds} | county: {county}"
            ),
        )
        counts["new_records"] += 1

    return output_rows, counts


def write_report(rows: list[dict[str, str]], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(
    counts: dict[str, int],
    output_path: Path,
    total_monami: int,
    total_json: int,
    active_licenses: int,
) -> None:
    print()
    print("=" * 60)
    print("NURSING HOME DIFF SUMMARY")
    print("=" * 60)
    print(f"Mon Ami records:          {total_monami}")
    print(f"DSHS source records:    {total_json} ({active_licenses} active licenses)")
    print(f"Matched by license ID:    {counts['matched_by_license']}")
    print(f"Matched by fuzzy match:   {counts['matched_by_fuzzy']}")
    print(f"Unchanged (OK):           {counts['unchanged']}")
    print(f"Field differences:        {counts['field_diffs']} (rows)")
    print(f"License renewed (CHOW):   {counts['license_renewed']}")
    print(f"Archived (flag):          {counts['archived']}")
    print(f"Duplicate Mon Ami rows:   {counts['duplicate_monami']}")
    print(f"Unmatched in DSHS:        {counts['unmatched_monami']}")
    print(f"NEW — add to Mon Ami:     {counts['new_records']}")
    print("=" * 60)
    print(f"Full report written to: {output_path}")
    if counts["new_records"]:
        print(f"\n>>> {counts['new_records']} record(s) should be ADDED to Mon Ami.")
    if counts["license_renewed"]:
        print(
            f">>> {counts['license_renewed']} record(s) have a new license at the same location "
            f"(update, don't add)."
        )
    if counts["duplicate_monami"]:
        print(
            f">>> {counts['duplicate_monami']} Mon Ami row(s) are duplicates of another entry "
            f"already matched to DSHS."
        )
    if counts["archived"]:
        print(f">>> {counts['archived']} record(s) should be ARCHIVED in Mon Ami.")
    if counts["field_diffs"]:
        print(f">>> {counts['field_diffs']} field difference row(s) to review.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff Mon Ami nursing homes CSV against WA DSHS facility data."
    )
    parser.add_argument(
        "--monami",
        type=Path,
        default=Path("monaminursing.csv"),
        help="Path to monaminursing.csv (default: monaminursing.csv)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Path to local DSHS data file (default: query.json when not fetching)",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch latest data from DSHS ArcGIS instead of using local file",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("diff_report.csv"),
        help="Output CSV path (default: diff_report.csv)",
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=0.75,
        help="Minimum combined name+address similarity for fuzzy match (default: 0.75)",
    )
    parser.add_argument(
        "--include-ok",
        action="store_true",
        help="Include OK (unchanged) rows in output (default: only when there are no issues)",
    )
    args = parser.parse_args()

    if not args.monami.exists():
        print(f"Error: Mon Ami file not found: {args.monami}", file=sys.stderr)
        return 1

    json_path = args.json
    if not args.fetch and json_path is None:
        json_path = Path("query.json")
    if not args.fetch and json_path and not json_path.exists():
        print(f"Error: DSHS data file not found: {json_path}", file=sys.stderr)
        return 1

    monami_rows = load_monami(args.monami)
    geojson = load_geojson(json_path, args.fetch)
    rows, counts = run_diff(monami_rows, geojson, args.fuzzy_threshold)
    _, _, active_by_license, _ = build_json_indexes(geojson.get("features", []))

    has_issues = any(
        counts[k]
        for k in ("field_diffs", "archived", "license_renewed", "duplicate_monami", "unmatched_monami", "new_records")
    )
    if not args.include_ok and not has_issues:
        rows = [r for r in rows if r["status"] != "OK"]
    elif not args.include_ok and has_issues:
        rows = [r for r in rows if r["status"] != "OK"]

    write_report(rows, args.output)
    print_summary(
        counts,
        args.output,
        len(monami_rows),
        len(geojson.get("features", [])),
        len(active_by_license),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

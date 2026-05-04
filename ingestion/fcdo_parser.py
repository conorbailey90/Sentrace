"""
FCDO UK Sanctions List parser for fractal.
"""

import requests
import csv
import io
from collections import defaultdict

from ingestion.name_utils import strip_company_suffix

FCDO_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"

entity_type_map = {
    "individual": "individual",
    "entity": "organisation",
    "ship": "vessel",
}


def download_fcdo():
    print("Downloading UK FCDO Sanctions list...")
    response = requests.get(FCDO_URL, timeout=60)
    response.raise_for_status()
    return response.content


def extract_list_date(raw_content):
    """Extract a date from the FCDO CSV metadata line (line 0)."""
    if isinstance(raw_content, io.IOBase):
        raw_content = raw_content.read()
    if isinstance(raw_content, (bytes, bytearray)):
        raw_text = raw_content.decode("utf-8", errors="replace")
    else:
        raw_text = raw_content
    first_line = raw_text.splitlines()[0] if raw_text.strip() else ""
    parts = first_line.split(':', 1)
    return parts[1].strip() if len(parts) > 1 else None


def build_name(row):
    """Build 'Last, First Middle...' from Name columns."""
    name6 = row.get("Name 6", "").strip()
    given = " ".join([
        row.get(f"Name {i}", "").strip()
        for i in range(1, 6)
        if row.get(f"Name {i}", "").strip()
    ])
    if given:
        return f"{name6}, {given}"
    return name6


def generate_individual_variants(row):
    """Generate name variants for an individual."""
    variants = []
    seen = set()

    surname = row.get("Name 6", "").strip()
    given_parts = [
        row.get(f"Name {i}", "").strip()
        for i in range(1, 6)
        if row.get(f"Name {i}", "").strip()
    ]

    first_name = given_parts[0] if given_parts else ""
    middle_names = given_parts[1:] if len(given_parts) > 1 else []

    def add(name, variant_type):
        cleaned = name.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            variants.append((cleaned, variant_type))

    if surname and given_parts:
        add(f"{surname}, {' '.join(given_parts)}", "full_name")

    if surname and first_name:
        add(f"{surname}, {first_name}", "first_last")

    if surname and given_parts:
        add(f"{' '.join(given_parts)} {surname}", "natural_order")

    if surname and first_name:
        add(f"{first_name} {surname}", "natural_first_last")

    if surname:
        add(surname, "surname_only")

    for middle in middle_names:
        add(f"{first_name} {middle} {surname}", "partial_middle")
        add(f"{surname}, {first_name} {middle}", "partial_middle")

    return variants


def add_variants(all_name_variants, row, entity_type, source, seen_variants):
    """Generate and append name variants based on entity type."""
    if entity_type == "individual":
        for variant, variant_type in generate_individual_variants(row):
            key = variant.lower()
            if key not in seen_variants:
                seen_variants.add(key)
                all_name_variants.append({
                    "name": variant,
                    "variant_type": variant_type,
                    "source": source,
                })
    else:
        # Organisations and vessels — store the full name + a stripped version
        name = build_name(row)
        if not name:
            return

        key = name.lower()
        if key not in seen_variants:
            seen_variants.add(key)
            all_name_variants.append({
                "name": name,
                "variant_type": "full_name",
                "source": source,
            })

        # Add stripped version if a company suffix was removed
        stripped, changed = strip_company_suffix(name)
        if changed:
            stripped_key = stripped.lower()
            if stripped_key not in seen_variants:
                seen_variants.add(stripped_key)
                all_name_variants.append({
                    "name": stripped,
                    "variant_type": "stripped_suffix",
                    "source": source,
                })


def parse_fcdo(source):
    if isinstance(source, (bytes, bytearray)):
        source = source.decode("utf-8", errors="replace")
    lines = source.splitlines()
    csv_text = "\n".join(lines[1:])  # strip metadata first line
    reader = csv.DictReader(io.StringIO(csv_text))
    grouped = defaultdict(list)

    for row in reader:
        uid = row.get("Unique ID", "").strip()
        if uid:
            grouped[uid].append(row)

    for uid, rows in grouped.items():
        primary_name = None
        primary_row = None
        entity_type = None
        all_name_variants = []
        seen_aliases = set()
        seen_variants = set()

        for row in rows:
            name = build_name(row)
            name_type = row.get("Name type", "").strip().lower()

            if not entity_type:
                entity_type = entity_type_map.get(
                    row.get("Designation Type", "").strip().lower(),
                    "organisation"
                )

            if name_type == "primary name" and not primary_name:
                primary_name = name
                primary_row = row
                seen_aliases.add(name.lower())
                add_variants(all_name_variants, row, entity_type, "primary", seen_variants)

            elif name:
                alias_key = name.lower()
                if alias_key not in seen_aliases:
                    seen_aliases.add(alias_key)
                    add_variants(
                        all_name_variants, row, entity_type,
                        f"alias ({name_type})", seen_variants
                    )

        if not primary_name:
            continue

        yield {
            "source_list": "FCDO",
            "source_id": uid,
            "entity_type": entity_type,
            "primary_name": primary_name,
            "country": primary_row.get("Address Country", "").strip() or None,
            "date_of_birth": primary_row.get("D.O.B", "").strip() or None,
            "nationality": primary_row.get("Nationality(/ies)", "").strip() or None,
            "date_listed": primary_row.get("Date Designated", "").strip() or None,
            "is_active": True,
            "date_delisted": None,
            "last_updated": primary_row.get("Last Updated", "").strip() or None,
            "remarks": " | ".join(filter(None, [
                primary_row.get("Other Information", "").strip(),
                primary_row.get("UK Statement of Reasons", "").strip(),
            ])) or None,
            "programs": [
                primary_row.get("Regime Name", "").strip()
            ] if primary_row.get("Regime Name", "").strip() else [],
            "name_variants": all_name_variants,
        }


if __name__ == "__main__":
    csv_cont = download_fcdo()
    date = extract_list_date(csv_cont)
    results = list(parse_fcdo(csv_cont)) 

    individuals = [e for e in results if e["entity_type"] == "individual"]
    orgs = [e for e in results if e["entity_type"] == "organisation"]
    vessels = [e for e in results if e["entity_type"] == "vessel"]

    print(f"Total entities: {len(results)}")
    print(f"  Individuals:   {len(individuals)}")
    print(f"  Organisations: {len(orgs)}")
    print(f"  Vessels:       {len(vessels)}")

    total_variants = sum(len(e["name_variants"]) for e in results)
    print(f"Total name variants: {total_variants}")

    print("\n--- Individual example ---")
    if individuals:
        e = individuals[0]
        print(f"Entity: {e['primary_name']} ({e['source_id']})")
        for v in e["name_variants"]:
            print(f"  [{v['variant_type']:20s}] {v['name']:40s} ({v['source']})")

    print("\n--- Organisation example ---")
    if orgs:
        e = orgs[0]
        print(f"Entity: {e['primary_name']} ({e['source_id']})")
        for v in e["name_variants"]:
            print(f"  [{v['variant_type']:20s}] {v['name']:40s} ({v['source']})")

    # Find an org with a strippable suffix to demo
    print("\n--- Organisation with stripped suffix example ---")
    for e in orgs:
        if any(v["variant_type"] == "stripped_suffix" for v in e["name_variants"]):
            print(f"Entity: {e['primary_name']} ({e['source_id']})")
            for v in e["name_variants"]:
                print(f"  [{v['variant_type']:20s}] {v['name']:40s} ({v['source']})")
            break
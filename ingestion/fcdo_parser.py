import requests
import csv
import io
from collections import defaultdict

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
    lines = response.text.splitlines()
    csv_content = "\n".join(lines[1:])
    return csv_content

def build_name(row):
    name6 = row.get("Name 6", "").strip()
    given = " ".join([
        row.get(f"Name {i}", "").strip()
        for i in range(1, 6)
        if row.get(f"Name {i}", "").strip()
    ])
    if given:
        return f"{name6}, {given}"
    return name6


def parse_fcdo(csv_text):

    reader = csv.DictReader(io.StringIO(csv_text))

    grouped = defaultdict(list)

    for row in reader:
        uid = row.get("Unique ID", "").strip()
        if uid:
            grouped[uid].append(row)

    for uid, rows in grouped.items():
        seen_names = set()
        primary_name = None
        primary_row = None
        aliases = []

        for row in rows:
            name = build_name(row)
            if name and name not in seen_names:
                seen_names.add(name)
                name_type = row.get("Name type", "").strip().lower()
                if name_type == "primary name":
                    primary_name = name
                    primary_row = row  # keep for extracting other fields
            
                else:
                    aliases.append({
                        "alias_name": name,
                        "alias_type": name_type
                    })
        if not primary_name:
            continue

        yield {
            "source_list": "FCDO",
            "source_id": uid,
            "entity_type": entity_type_map.get(primary_row.get("Designation Type", "").strip().lower(), "organisation"),
            "primary_name": primary_name,
            "country": primary_row.get("Address Country", "").strip() or None,
            "date_of_birth": primary_row.get("D.O.B", "").strip() or None,
            "nationality": primary_row.get("Nationality(/ies)", "").strip() or None,
            "date_listed": primary_row.get("Date Designated", "").strip() or None,
            "is_active": True,
            "date_delisted": None,
            "last_updated": primary_row.get("Last Updated", "").strip() or None,
            "programs": [primary_row.get("Regime Name", "").strip()] if primary_row.get("Regime Name", "").strip() else [],
            "aliases": aliases,
        }


csv_cont = download_fcdo()
results = list(parse_fcdo(csv_cont))

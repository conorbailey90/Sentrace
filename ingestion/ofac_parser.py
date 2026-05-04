"""
OFAC SDN Enhanced XML parser for fractal.
Outputs the same dict shape as parse_fcdo / parse_eu.
"""

import requests
import xml.etree.ElementTree as ET

from ingestion.name_utils import strip_company_suffix

OFAC_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ENHANCED.XML"

NS = {"o": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ENHANCED_XML"}

ENTITY_TYPE_MAP = {
    "individual": "individual",
    "entity": "organisation",
    "vessel": "vessel",
    "aircraft": "aircraft",
}

# Feature type IDs (stable — preferred over element text for features)
FEATURE_BIRTHDATE = "8"
FEATURE_NATIONALITY = "10"

# Script refId for Latin
LATIN_SCRIPT_ID = "20122"


def q(tag):
    """Shorthand for namespaced element tag."""
    return f"{{{NS['o']}}}{tag}"


def text(elem):
    """Safely get stripped text from an element, or empty string."""
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def download_ofac():
    print("Downloading OFAC SDN Enhanced XML...")
    response = requests.get(OFAC_URL, timeout=120)
    response.raise_for_status()
    return response.content


def get_latin_translation(name_elem):
    """Find the Latin-script translation within a <name> element."""
    translations = name_elem.find(q("translations"))
    if translations is None:
        return None
    for translation in translations.findall(q("translation")):
        script = translation.find(q("script"))
        if script is not None and script.get("refId") == LATIN_SCRIPT_ID:
            return translation
    return None


def extract_name_parts(translation):
    """Return dict of name parts keyed by type."""
    parts = {"first": "", "middle": "", "last": "", "entity_name": ""}

    name_parts_elem = translation.find(q("nameParts"))
    if name_parts_elem is None:
        return parts

    for np in name_parts_elem.findall(q("namePart")):
        type_name = text(np.find(q("type")))
        value = text(np.find(q("value")))
        if not value:
            continue

        if type_name == "First Name":
            parts["first"] = value
        elif type_name == "Middle Name":
            parts["middle"] = value
        elif type_name == "Last Name":
            parts["last"] = value
        elif type_name in ("Entity Name", "Vessel Name", "Aircraft Name"):
            parts["entity_name"] = value

    return parts


def generate_individual_variants(parts):
    """Generate name variants for an individual from structured parts."""
    variants = []
    seen = set()

    first = parts["first"]
    middle = parts["middle"]
    last = parts["last"]
    middle_names = middle.split() if middle else []

    def add(name, variant_type):
        cleaned = name.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            variants.append((cleaned, variant_type))

    if last and (first or middle):
        given = " ".join(filter(None, [first, middle]))
        add(f"{last}, {given}", "full_name")

    if last and first:
        add(f"{last}, {first}", "first_last")

    if last and (first or middle):
        given = " ".join(filter(None, [first, middle]))
        add(f"{given} {last}", "natural_order")

    if last and first:
        add(f"{first} {last}", "natural_first_last")

    if last:
        add(last, "surname_only")

    for m in middle_names:
        if first and last:
            add(f"{first} {m} {last}", "partial_middle")
            add(f"{last}, {first} {m}", "partial_middle")

    return variants


def build_formatted_name(translation):
    """Build the display name from a translation."""
    full = text(translation.find(q("formattedFullName")))
    if full:
        return full
    parts = extract_name_parts(translation)
    if parts["entity_name"]:
        return parts["entity_name"]
    if parts["last"]:
        given = " ".join(filter(None, [parts["first"], parts["middle"]]))
        return f"{parts['last']}, {given}" if given else parts["last"]
    return ""


def add_variants(all_name_variants, translation, entity_type, source, seen_variants):
    """Generate and append variants based on entity type."""
    if entity_type == "individual":
        parts = extract_name_parts(translation)
        for variant, variant_type in generate_individual_variants(parts):
            key = variant.lower()
            if key not in seen_variants:
                seen_variants.add(key)
                all_name_variants.append({
                    "name": variant,
                    "variant_type": variant_type,
                    "source": source,
                })
    else:
        # Organisations, vessels, aircraft — store full name + stripped version
        name = build_formatted_name(translation)
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


def extract_features(entity_elem):
    """Extract primary DOB and nationality from features."""
    result = {"date_of_birth": None, "nationality": None}
    features = entity_elem.find(q("features"))
    if features is None:
        return result

    for feature in features.findall(q("feature")):
        type_elem = feature.find(q("type"))
        value = text(feature.find(q("value")))
        is_primary = text(feature.find(q("isPrimary"))) == "true"

        if not is_primary or type_elem is None or not value:
            continue

        feature_type_id = type_elem.get("featureTypeId")

        if feature_type_id == FEATURE_BIRTHDATE and not result["date_of_birth"]:
            result["date_of_birth"] = value
        elif feature_type_id == FEATURE_NATIONALITY and not result["nationality"]:
            result["nationality"] = value

    return result


def extract_country(entity_elem):
    """First country from addresses."""
    addresses = entity_elem.find(q("addresses"))
    if addresses is None:
        return None
    for address in addresses.findall(q("address")):
        country = text(address.find(q("country")))
        if country:
            return country
    return None


def extract_programs(entity_elem):
    """All sanctions program names."""
    programs = []
    programs_elem = entity_elem.find(q("sanctionsPrograms"))
    if programs_elem is None:
        return programs
    for program in programs_elem.findall(q("sanctionsProgram")):
        value = text(program)
        if value:
            programs.append(value)
    return programs


def extract_date_listed(entity_elem):
    """datePublished from the first sanctionsList entry."""
    lists_elem = entity_elem.find(q("sanctionsLists"))
    if lists_elem is None:
        return None
    first = lists_elem.find(q("sanctionsList"))
    if first is not None:
        return first.get("datePublished")
    return None


def extract_remarks(entity_elem):
    """Free-text remarks from the <remarks> element."""
    remarks_elem = entity_elem.find(q("remarks"))
    if remarks_elem is None or not remarks_elem.text:
        return None
    return remarks_elem.text.strip() or None


def parse_entity(entity_elem):
    """Parse a single <entity> element. Returns dict or None."""
    source_id = entity_elem.get("id")
    if not source_id:
        return None

    general_info = entity_elem.find(q("generalInfo"))
    if general_info is None:
        return None

    entity_type_text = text(general_info.find(q("entityType"))).lower()
    entity_type = ENTITY_TYPE_MAP.get(entity_type_text, "organisation")

    names_elem = entity_elem.find(q("names"))
    if names_elem is None:
        return None

    primary_name = None
    all_name_variants = []
    seen_aliases = set()
    seen_variants = set()

    for name_elem in names_elem.findall(q("name")):
        is_primary = text(name_elem.find(q("isPrimary"))) == "true"
        alias_type = text(name_elem.find(q("aliasType"))).lower()

        translation = get_latin_translation(name_elem)
        if translation is None:
            continue

        formatted = build_formatted_name(translation)
        if not formatted:
            continue

        if is_primary and not primary_name:
            primary_name = formatted
            seen_aliases.add(formatted.lower())
            add_variants(all_name_variants, translation, entity_type, "primary", seen_variants)
        else:
            alias_key = formatted.lower()
            if alias_key not in seen_aliases:
                seen_aliases.add(alias_key)
                source_label = f"alias ({alias_type})" if alias_type else "alias"
                add_variants(all_name_variants, translation, entity_type, source_label, seen_variants)

    if not primary_name:
        return None

    features = extract_features(entity_elem)

    return {
        "source_list": "OFAC_SDN",
        "source_id": source_id,
        "entity_type": entity_type,
        "primary_name": primary_name,
        "country": extract_country(entity_elem),
        "date_of_birth": features["date_of_birth"],
        "nationality": features["nationality"],
        "date_listed": extract_date_listed(entity_elem),
        "is_active": True,
        "date_delisted": None,
        "last_updated": None,
        "remarks": extract_remarks(entity_elem),
        "programs": extract_programs(entity_elem),
        "name_variants": all_name_variants,
    }


def extract_list_date(source):
    """Return the dataAsOf date from <publicationInfo><dataAsOf>, or None."""
    if isinstance(source, (bytes, bytearray)):
        import io
        source = io.BytesIO(source)

    target = q("dataAsOf")
    for _, elem in ET.iterparse(source, events=("end",)):
        if elem.tag == target:
            value = text(elem)
            if value:
                # Strip timezone offset to return a plain date string
                return value.split("T")[0]
        elem.clear()
    return None


def parse_ofac(source):
    """Parse the OFAC SDN Enhanced XML.

    `source` may be a file path, a file-like object, or bytes.
    Yields one dict per entity. Uses iterparse to keep memory low.
    """
    if isinstance(source, (bytes, bytearray)):
        import io
        source = io.BytesIO(source)

    for event, elem in ET.iterparse(source, events=("end",)):
        if elem.tag != q("entity"):
            continue
        try:
            parsed = parse_entity(elem)
            if parsed is not None:
                yield parsed
        finally:
            elem.clear()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        path_or_bytes = sys.argv[1]
        print(f"Parsing OFAC data from {path_or_bytes}...")
    else:
        path_or_bytes = download_ofac()
        print("Parsing OFAC data...")

    results = list(parse_ofac(path_or_bytes))

    individuals = [e for e in results if e["entity_type"] == "individual"]
    orgs = [e for e in results if e["entity_type"] == "organisation"]
    vessels = [e for e in results if e["entity_type"] == "vessel"]
    aircraft = [e for e in results if e["entity_type"] == "aircraft"]

    print(f"\nTotal entities: {len(results)}")
    print(f"  Individuals:   {len(individuals)}")
    print(f"  Organisations: {len(orgs)}")
    print(f"  Vessels:       {len(vessels)}")
    print(f"  Aircraft:      {len(aircraft)}")

    total_variants = sum(len(e["name_variants"]) for e in results)
    print(f"Total name variants: {total_variants}")

    print("\n--- Individual example ---")
    if individuals:
        e = individuals[0]
        print(f"Entity: {e['primary_name']} ({e['source_id']})")
        print(f"  DOB: {e['date_of_birth']}, Nationality: {e['nationality']}")
        print(f"  Programs: {e['programs']}")
        for v in e["name_variants"][:10]:
            print(f"  [{v['variant_type']:20s}] {v['name']:40s} ({v['source']})")

    print("\n--- Organisation with stripped suffix example ---")
    for e in orgs:
        if any(v["variant_type"] == "stripped_suffix" for v in e["name_variants"]):
            print(f"Entity: {e['primary_name']} ({e['source_id']})")
            print(f"  Country: {e['country']}, Programs: {e['programs']}")
            for v in e["name_variants"]:
                print(f"  [{v['variant_type']:20s}] {v['name']:40s} ({v['source']})")
            break
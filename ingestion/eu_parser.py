"""
EU Consolidated Financial Sanctions List XML parser for fractal.
Outputs the same dict shape as parse_ofac / parse_fcdo.
"""
import os
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
load_dotenv()

from ingestion.name_utils import strip_company_suffix

EU_BASE_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"

NS = {"e": "http://eu.europa.ec/fpi/fsd/export"}

SUBJECT_TYPE_MAP = {
    "person": "individual",
    "enterprise": "organisation",
}

# Languages that use Latin script (ISO 639-1 codes as they appear in the EU XML).
# Empty string is also accepted since it overwhelmingly indicates Latin
# transliteration in this dataset.
LATIN_LANGUAGES = {
    "", "EN", "FR", "DE", "ES", "IT", "PT", "NL", "SV", "DA", "NO", "FI",
    "PL", "CS", "SK", "HU", "RO", "HR", "SL", "ET", "LV", "LT", "MT", "GA",
    "TR", "VI", "ID", "MS", "SQ", "AZ", "UZ",
}


def q(tag):
    """Shorthand for namespaced element tag."""
    return f"{{{NS['e']}}}{tag}"


def download_eu():
    print("Downloading EU Consolidated Financial Sanctions List XML...")
    token = os.environ.get("EU_CFSL_TOKEN")
    url = f"{EU_BASE_URL}?token={token}" if token else EU_BASE_URL
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response.content


def attr(elem, name, default=""):
    """Safely read an attribute, returning default if elem is None or attr missing."""
    if elem is None:
        return default
    return (elem.get(name) or default).strip()


def _text_is_latin(s):
    """Return True if every letter in s falls within the Latin Unicode ranges."""
    return all(ord(c) <= 0x024F or not c.isalpha() for c in s)


def is_latin_alias(alias_elem):
    """True if the alias's nameLanguage is an explicit Latin-script code,
    or if unspecified and the actual text content is Latin."""
    lang = (alias_elem.get("nameLanguage") or "").strip().upper()
    if lang and lang in LATIN_LANGUAGES:
        return True
    if lang == "":
        return _text_is_latin(build_formatted_name(alias_elem))
    return False


def extract_name_parts(alias_elem):
    """Return dict of name parts keyed by type, mirroring parse_ofac."""
    return {
        "first": attr(alias_elem, "firstName"),
        "middle": attr(alias_elem, "middleName"),
        "last": attr(alias_elem, "lastName"),
        "entity_name": attr(alias_elem, "wholeName"),
    }


def generate_individual_variants(parts):
    """Generate name variants for an individual from structured parts.

    Identical logic to parse_ofac.generate_individual_variants — kept inline
    rather than imported to keep the parsers independent.
    """
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


def build_formatted_name(alias_elem):
    """Build the display name from a nameAlias element."""
    whole = attr(alias_elem, "wholeName")
    if whole:
        return whole
    parts = extract_name_parts(alias_elem)
    if parts["last"]:
        given = " ".join(filter(None, [parts["first"], parts["middle"]]))
        return f"{parts['last']}, {given}" if given else parts["last"]
    return parts["entity_name"]


def pick_primary_alias(aliases):
    """Pick the most authoritative Latin-script alias as the primary name.

    Preference order:
      1. nameLanguage == "EN"
      2. nameLanguage empty (typically Latin transliteration of the original)
      3. First Latin-script alias of any other language
      4. First alias overall (fallback so we never lose an entity)
    """
    en_aliases = []
    empty_aliases = []
    other_latin = []

    for a in aliases:
        lang = (a.get("nameLanguage") or "").strip().upper()
        if lang == "EN":
            en_aliases.append(a)
        elif lang == "":
            if _text_is_latin(build_formatted_name(a)):
                empty_aliases.append(a)
        elif lang in LATIN_LANGUAGES:
            other_latin.append(a)

    for bucket in (en_aliases, empty_aliases, other_latin, aliases):
        for a in bucket:
            if build_formatted_name(a):
                return a
    return None


def add_variants(all_name_variants, alias_elem, entity_type, source, seen_variants):
    """Generate and append variants based on entity type."""
    if entity_type == "individual":
        parts = extract_name_parts(alias_elem)
        generated = generate_individual_variants(parts)
        if not generated:
            # EU sometimes only provides wholeName with no split parts — store it directly
            name = build_formatted_name(alias_elem)
            if name:
                key = name.lower()
                if key not in seen_variants:
                    seen_variants.add(key)
                    all_name_variants.append({
                        "name": name,
                        "variant_type": "full_name",
                        "source": source,
                    })
            return
        for variant, variant_type in generated:
            key = variant.lower()
            if key not in seen_variants:
                seen_variants.add(key)
                all_name_variants.append({
                    "name": variant,
                    "variant_type": variant_type,
                    "source": source,
                })
    else:
        # Organisations — store wholeName and a suffix-stripped version
        name = build_formatted_name(alias_elem)
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


def extract_dob(entity_elem):
    """First birthdate. Prefer the ISO `birthdate` attribute, else compose from y/m/d."""
    bd = entity_elem.find(q("birthdate"))
    if bd is None:
        return None
    iso = attr(bd, "birthdate")
    if iso:
        return iso
    year = attr(bd, "year")
    if not year:
        return None
    month = attr(bd, "monthOfYear")
    day = attr(bd, "dayOfMonth")
    if month and day:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{year}-{int(month):02d}"
    return year


def extract_nationality(entity_elem):
    """First citizenship country description."""
    cit = entity_elem.find(q("citizenship"))
    return attr(cit, "countryDescription") or None


def extract_country(entity_elem):
    """First address country, falling back to citizenship country."""
    addr = entity_elem.find(q("address"))
    country = attr(addr, "countryDescription")
    if country:
        return country
    return extract_nationality(entity_elem)


def extract_programs(entity_elem):
    """Programme codes from regulation elements."""
    programs = []
    for reg in entity_elem.findall(q("regulation")):
        prog = attr(reg, "programme")
        if prog and prog not in programs:
            programs.append(prog)
    return programs


def extract_date_listed(entity_elem):
    """publicationDate from the first regulation element."""
    reg = entity_elem.find(q("regulation"))
    return attr(reg, "publicationDate") or None


def extract_remarks(entity_elem):
    """Free-text remark from the <remark> element."""
    remark_elem = entity_elem.find(q("remark"))
    if remark_elem is None or not remark_elem.text:
        return None
    return remark_elem.text.strip() or None


def parse_entity(entity_elem):
    """Parse a single <sanctionEntity> element. Returns dict or None."""
    source_id = entity_elem.get("euReferenceNumber") or entity_elem.get("logicalId")
    subject = entity_elem.find(q("subjectType"))
    subject_code = attr(subject, "code").lower()
    entity_type = SUBJECT_TYPE_MAP.get(subject_code, "organisation")

    aliases = entity_elem.findall(q("nameAlias"))
    if not aliases:
        return None

    primary_alias = pick_primary_alias(aliases)
    if primary_alias is None:
        return None

    primary_name = build_formatted_name(primary_alias)
    if not primary_name:
        return None

    all_name_variants = []
    seen_aliases = {primary_name.lower()}
    seen_variants = set()

    add_variants(all_name_variants, primary_alias, entity_type, "primary", seen_variants)

    for a in aliases:
        if a is primary_alias:
            continue
        if not is_latin_alias(a):
            continue
        formatted = build_formatted_name(a)
        if not formatted:
            continue
        alias_key = formatted.lower()
        if alias_key in seen_aliases:
            continue
        seen_aliases.add(alias_key)

        lang = (a.get("nameLanguage") or "").strip().lower()
        source_label = f"alias ({lang})" if lang else "alias"
        add_variants(all_name_variants, a, entity_type, source_label, seen_variants)

    return {
        "source_list": "EU_CFSL",
        "source_id": source_id,
        "entity_type": entity_type,
        "primary_name": primary_name,
        "country": extract_country(entity_elem),
        "date_of_birth": extract_dob(entity_elem),
        "nationality": extract_nationality(entity_elem),
        "date_listed": extract_date_listed(entity_elem),
        "is_active": True,
        "date_delisted": None,
        "last_updated": None,
        "remarks": extract_remarks(entity_elem),
        "programs": extract_programs(entity_elem),
        "name_variants": all_name_variants,
    }


def extract_list_date(source):
    """Return the generationDate attribute from the EU CFSL XML root, or None."""
    if isinstance(source, (bytes, bytearray)):
        import io
        source = io.BytesIO(source)

    for event, elem in ET.iterparse(source, events=("start",)):
        # First start event is always the root element
        date = elem.get("generationDate")
        return date.strip() if date else None
    return None


def parse_eu(source):
    """Parse the EU CFSL XML.

    `source` may be a file path, a file-like object, or bytes.
    Yields one dict per sanctioned entity. Uses iterparse to keep memory low.
    """
    if isinstance(source, (bytes, bytearray)):
        import io
        source = io.BytesIO(source)

    for event, elem in ET.iterparse(source, events=("end",)):
        if elem.tag != q("sanctionEntity"):
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
        print(f"Parsing EU CFSL data from {path_or_bytes}...")
    else:
        path_or_bytes = download_eu()
        print("Parsing EU CFSL data...")

    results = list(parse_eu(path_or_bytes))

    individuals = [e for e in results if e["entity_type"] == "individual"]
    orgs = [e for e in results if e["entity_type"] == "organisation"]

    print(f"\nTotal entities: {len(results)}")
    print(f"  Individuals:   {len(individuals)}")
    print(f"  Organisations: {len(orgs)}")

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
# This parser only gets EU asset freeze targets. Vessels are listed on a separate list. See the official EU Sanctions map: https://sanctionsmap.eu/#/main

import requests
import xml.etree.ElementTree as ET
import re

def is_latin(text):
    return bool(re.match(r'^[\x00-\x7F\s]+$', text))

# Token is a public access token — if this URL breaks check:
# https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions
EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"

NS = "http://eu.europa.ec/fpi/fsd/export"

entity_type_map = {
    "person": "individual",
    "enterprise": "organisation",
    "vessel": "vessel",
    "aircraft": "aircraft",
}

def download_eu():
    print("Downloading EU Sanctions list...")
    response = requests.get(EU_URL, timeout=60)
    response.raise_for_status()
    return ET.fromstring(response.content)

def parse_eu(root):
    for entry in root.iter(f"{{{NS}}}sanctionEntity"):
        uid = entry.get("euReferenceNumber")
        
        primary_name_found = False
        primary_name = None
        aliases = []
    
        raw_type = entry.find(f"{{{NS}}}subjectType").get("code")
        entity_type = entity_type_map.get(raw_type, "organisation")

        regulation_el = entry.find(f"{{{NS}}}regulation")
        programme = regulation_el.get("programme") if regulation_el is not None else None
        last_update = regulation_el.get("publicationDate") if regulation_el is not None else None

        designation_date = entry.get("designationDate")

        dob_el = entry.find(f"{{{NS}}}birthdate")
        if dob_el is not None:
            dob = dob_el.get("birthdate") or dob_el.get("year")
            if dob and dob_el.get("circa") == "true":
                dob = f"circa {dob}"
        else:
            dob = None

        nationality = None
        if entity_type == "individual":
            nationality_el = entry.find(f"{{{NS}}}citizenship")
            if nationality_el is not None:
                nationality = nationality_el.get("countryDescription") or None

        country = None
        address_el = entry.find(f"{{{NS}}}address")
        if address_el is not None:
            country = address_el.get("countryDescription") or None

        for name in entry.iter(f"{{{NS}}}nameAlias"):
            whole_name = name.get("wholeName", "").strip()
            if not whole_name:
                continue
            if is_latin(whole_name) and not primary_name_found:
                primary_name = whole_name
                primary_name_found = True
            else:
                aliases.append({
                    "alias_name": whole_name,
                    "alias_type": "aka"
                })

        if not primary_name:
            continue

        yield {
            "source_list": "EU",
            "source_id": uid,
            "entity_type": entity_type,
            "primary_name": primary_name,
            "country": country,
            "date_of_birth": dob,
            "nationality": nationality,
            "date_listed": designation_date,
            "is_active": True,
            "date_delisted": None,
            "last_updated": last_update,
            "programs": [programme] if programme else [],
            "aliases": aliases,
        }


xml_cont = download_eu()
results = list(parse_eu(xml_cont))
for result in results:
    print(result['programs'])
import requests
import xml.etree.ElementTree as ET

OFAC_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"

NS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"


entity_type_map = {
    "Individual": "individual",
    "Entity": "organisation",
    "Vessel": "vessel",
    "Aircraft": "aircraft",
}

def download_ofac():
    print("Downloading OFAC SDN list...")
    response = requests.get(OFAC_URL, timeout=60)
    response.raise_for_status()
    return ET.fromstring(response.content)

def parse_ofac(root):

    for entry in root.iter(f"{{{NS}}}sdnEntry"):
        uid = entry.find(f"{{{NS}}}uid")
        first_name = entry.find(f"{{{NS}}}firstName")
        last_name = entry.find(f"{{{NS}}}lastName")
        sdn_type = entry.find(f"{{{NS}}}sdnType")
        program_list = entry.find(f"{{{NS}}}programList")
        dob_list = entry.find(f"{{{NS}}}dateOfBirthList")

        if uid is None or last_name is None:
            continue

        if first_name is not None and first_name.text:
            primary_name = f"{last_name.text}, {first_name.text}"
        else:
            primary_name = last_name.text

        raw_type = sdn_type.text if sdn_type is not None else "Entity"
        entity_type = entity_type_map.get(raw_type, "organisation")

        aliases = []
        aka_list = entry.find(f"{{{NS}}}akaList")

        if aka_list is not None:
            for aka in aka_list.iter(f"{{{NS}}}aka"):
                aka_last = aka.find(f"{{{NS}}}lastName")
                aka_first = aka.find(f"{{{NS}}}firstName")
                aka_type = aka.find(f"{{{NS}}}type")

                if aka_last is None:
                    continue

                if aka_first is not None and aka_first.text:
                    alias_name = f"{aka_last.text}, {aka_first.text}"
                else:
                    alias_name = aka_last.text

                aliases.append({
                    "alias_name": alias_name,
                    "alias_type": aka_type.text.lower() if aka_type is not None else "aka"
                })

        # Indiviuals

        nationality = None
        nationality_list = entry.find(f"{{{NS}}}nationalityList")

        if nationality_list is not None:
            country_el = nationality_list.find(f"{{{NS}}}nationality/{{{NS}}}country")
            if country_el is not None:
                nationality = country_el.text

        date_of_birth = None

        if dob_list is not None:
            for dob in dob_list.iter(f"{{{NS}}}dateOfBirthItem"):
                main_entry = dob.find(f"{{{NS}}}mainEntry")

                if main_entry is not None and main_entry.text == 'true':
                    date_of_birth = dob.find(f"{{{NS}}}dateOfBirth").text

        country = None
        address_list = entry.find(f"{{{NS}}}addressList")

        if address_list is not None:
            country_el = address_list.find(f"{{{NS}}}address/{{{NS}}}country")
            if country_el is not None:
                country = country_el.text

        programs = []

        if program_list is not None:
            for program in program_list.iter(f"{{{NS}}}program"):
                programs.append(program.text)

        yield {
            "source_list": "OFAC",
            "source_id": uid.text,
            "entity_type": entity_type,
            "primary_name": primary_name,
            "country": country,
            "date_of_birth": date_of_birth,
            "nationality": nationality,
            "date_listed": None,
            "is_active": True,
            "date_delisted": None,
            "last_updated": None,
            "programs": programs,
            "aliases": aliases,
        }


xml_cont = download_ofac()

results = list(parse_ofac(xml_cont))


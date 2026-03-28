from database import SessionLocal
from models.models import Listing, ListingAlias, ListingProgram
from ingestion.ofac_parser import download_ofac, parse_ofac
from ingestion.fcdo_parser import download_fcdo, parse_fcdo
from ingestion.eu_parser import download_eu, parse_eu


def load_entries(db, entries, source_list):
    print(f"Clearing existing {source_list} entries...")
    db.query(Listing).filter(Listing.source_list == source_list).delete()
    db.commit()

    print(f"Loading {source_list} entries...")
    count = 0

    for entry in entries:
        aliases = entry.pop("aliases")
        programs = entry.pop("programs")
        listing = Listing(**entry)
        db.add(listing)
        db.flush()

        for alias in aliases:
            db.add(ListingAlias(
                listing_id=listing.id,
                **alias
            ))

        for program in programs:
            db.add(ListingProgram(
                listing_id=listing.id,
                program_name=program
            ))

        count += 1
        if count % 500 == 0:
            db.commit()
            print(f"  {source_list}: {count} entries loaded...")

    db.commit()
    print(f"  Done — {count} {source_list} entries loaded.")


def ingest_all():
    db = SessionLocal()
    try:
        ofac_xml = download_ofac()
        ofac_results = parse_ofac(ofac_xml)
        load_entries(db, ofac_results, 'OFAC')

        fcdo_csv = download_fcdo()
        fcdo_results = parse_fcdo(fcdo_csv)
        load_entries(db, fcdo_results, 'FCDO')

        eu_xml = download_eu()
        eu_results = parse_eu(eu_xml)
        load_entries(db, eu_results, 'EU')

    except Exception as e:
        db.rollback()
        print(f"Ingestion failed: {e}")
        raise e
    finally:
        db.close()


ingest_all()
# Run from root with: PYTHONPATH=. python3 -m ingestion.loader

from dotenv import load_dotenv
load_dotenv()

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from database import SessionLocal, init_db
from models.sanctions import Entity, NameVariant, Program, ListSnapshot, EntityAudit
from ingestion.fcdo_parser import download_fcdo, parse_fcdo, extract_list_date as fcdo_list_date
from ingestion.ofac_parser import download_ofac, parse_ofac, extract_list_date as ofac_list_date
from ingestion.eu_parser import download_eu, parse_eu, extract_list_date as eu_list_date

# Raw list files are saved here so every historical version is recoverable.
ARCHIVE_DIR = Path(__file__).parent.parent / "list-archive"


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%d/%m/%Y",        # 01/05/2026  (FCDO, already target format)
    "%Y-%m-%d",        # 2026-05-01  (OFAC)
    "%Y-%m-%dT%H:%M:%S",  # 2026-05-01T00:00:00
    "%d %b %Y",        # 01 May 2026
    "%d-%b-%Y",        # 01-May-2026
    "%Y%m%d",          # 20260501
]


def _to_dd_mm_yyyy(value):
    """Normalise any recognised date string to DD/MM/YYYY, or return the original."""
    if not value or not isinstance(value, str):
        return value
    cleaned = value.strip()
    # Strip trailing timezone offset (e.g. "+01:00" or "-04:00")
    if len(cleaned) > 10 and cleaned[10] in ("T", " ") and (
        "+" in cleaned[10:] or cleaned.count("-") > 2
    ):
        cleaned = cleaned[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return value  # return as-is if nothing matched


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _compute_hash(raw_content):
    """
    Return the SHA-256 hex digest of the raw list content.

    SHA-256 works by:
      1. Treating the entire file as a stream of bytes.
      2. Splitting it into fixed-size blocks and running them through a
         series of bitwise operations defined by the SHA-256 standard.
      3. Producing a 256-bit (64 hex character) fingerprint that is
         deterministic — the same bytes always produce the same digest —
         and practically collision-free: two different files will not
         produce the same hash.

    We use this fingerprint to answer "did the list change since the last
    run?" without loading or parsing the previous file. If the hash matches
    the last successful snapshot's hash, the content is byte-for-byte
    identical and ingestion can be skipped.
    """
    if isinstance(raw_content, str):
        raw_content = raw_content.encode("utf-8")
    return hashlib.sha256(raw_content).hexdigest()


# ---------------------------------------------------------------------------
# Raw file archiving
# ---------------------------------------------------------------------------

def _archive_raw(source_list, raw_content, fetched_at):
    """Save raw content to list-archive/<source_list>/<timestamp>.<ext>."""
    ext = "csv" if source_list == "FCDO" else "xml"
    dir_path = ARCHIVE_DIR / source_list
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = f"{fetched_at.strftime('%Y-%m-%dT%H-%M-%S')}.{ext}"
    file_path = dir_path / filename
    if isinstance(raw_content, str):
        file_path.write_text(raw_content, encoding="utf-8")
    else:
        file_path.write_bytes(raw_content)
    return str(file_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scalar_fields(source):
    """
    Return a dict of the auditable scalar fields from either an entity dict
    (parser output) or an Entity ORM instance (current DB state).
    This dict is JSON-serialised and stored in entity_audit.previous_data
    and entity_audit.new_data so the full before/after state is queryable.
    """
    if isinstance(source, dict):
        keys = (
            "entity_type", "primary_name", "country", "date_of_birth",
            "nationality", "date_listed", "date_delisted", "last_updated",
            "is_active", "remarks",
        )
        return {k: source.get(k) for k in keys}
    return {
        "entity_type": source.entity_type,
        "primary_name": source.primary_name,
        "country": source.country,
        "date_of_birth": source.date_of_birth,
        "nationality": source.nationality,
        "date_listed": source.date_listed,
        "date_delisted": source.date_delisted,
        "last_updated": source.last_updated,
        "is_active": source.is_active,
        "remarks": source.remarks,
    }


# ---------------------------------------------------------------------------
# Core upsert
# ---------------------------------------------------------------------------

def upsert_entity(entity_data, existing_map):
    """
    Insert or update a single entity using an in-memory lookup (no DB queries).

    existing_map: dict of source_id -> Entity, preloaded for the whole source list.

    Returns (action, prev_data, new_data, entity) where:
      action    — "inserted" or "updated"
      prev_data — scalar-field dict captured BEFORE any changes (None for inserts)
      new_data  — scalar-field dict of the values being written
      entity    — the Entity ORM object (id populated after the next batch flush)
    """
    existing = existing_map.get(entity_data["source_id"])

    if existing:
        prev_data = _scalar_fields(existing)
        existing.entity_type = entity_data["entity_type"]
        existing.primary_name = entity_data["primary_name"]
        existing.country = entity_data["country"]
        existing.date_of_birth = entity_data["date_of_birth"]
        existing.nationality = entity_data["nationality"]
        existing.date_listed = entity_data["date_listed"]
        existing.date_delisted = entity_data["date_delisted"]
        existing.last_updated = entity_data["last_updated"]
        existing.is_active = entity_data["is_active"]
        existing.remarks = entity_data.get("remarks")
        entity = existing
        action = "updated"
    else:
        prev_data = None
        entity = Entity(
            source_list=entity_data["source_list"],
            source_id=entity_data["source_id"],
            entity_type=entity_data["entity_type"],
            primary_name=entity_data["primary_name"],
            country=entity_data["country"],
            date_of_birth=entity_data["date_of_birth"],
            nationality=entity_data["nationality"],
            date_listed=entity_data["date_listed"],
            date_delisted=entity_data["date_delisted"],
            last_updated=entity_data["last_updated"],
            is_active=entity_data["is_active"],
            remarks=entity_data.get("remarks"),
        )
        action = "inserted"

    for variant in entity_data["name_variants"]:
        entity.name_variants.append(NameVariant(
            name=variant["name"],
            variant_type=variant["variant_type"],
            source=variant["source"],
        ))
    for program_name in entity_data["programs"]:
        entity.programs.append(Program(program_name=program_name))

    new_data = _scalar_fields(entity_data)
    return action, prev_data, new_data, entity


# ---------------------------------------------------------------------------
# Batch loader
# ---------------------------------------------------------------------------

def _load_entities(session, label, source_list, entities, snapshot):
    """
    Upsert all entities for one source list and write EntityAudit records.

    Audit records are only written when data actually changed:
      - every "inserted" entity → change_type="added"
      - "updated" entities where scalar fields differ → change_type="updated"
    Entities that were active in the DB but absent from this snapshot are
    marked is_active=False → change_type="removed".

    Returns (inserted, updated, removed) counts where each counter reflects
    genuine data changes, not just rows touched.
    """
    inserted = 0
    updated = 0
    seen_source_ids = set()
    pending_audits = []
    now = snapshot.fetched_at

    # Preload all existing entities for this source list in one query.
    print(f"  [{label}] Loading existing entities...")
    existing_entities = session.query(Entity).filter_by(source_list=source_list).all()
    existing_map = {e.source_id: e for e in existing_entities}
    print(f"  [{label}] Found {len(existing_map)} existing entities")

    # First pass: classify each incoming entity as insert, update, or unchanged.
    to_insert = []   # list of entity_data dicts
    to_update = []   # list of (entity_data, existing Entity, prev_data)
    unchanged = []   # list of (entity_data, existing Entity)

    for entity_data in entities:
        seen_source_ids.add(entity_data["source_id"])
        existing = existing_map.get(entity_data["source_id"])
        if existing is None:
            to_insert.append(entity_data)
        else:
            prev_data = _scalar_fields(existing)
            new_data = _scalar_fields(entity_data)
            if prev_data != new_data:
                to_update.append((entity_data, existing, prev_data))
            else:
                unchanged.append((entity_data, existing))

    print(f"  [{label}] {len(to_insert)} inserts, {len(to_update)} updates, {len(unchanged)} unchanged")

    # Bulk-delete variants and programs only for entities that actually changed.
    if to_update:
        update_ids = [e.id for _, e, _ in to_update]
        session.query(NameVariant).filter(NameVariant.entity_id.in_(update_ids)).delete(synchronize_session=False)
        session.query(Program).filter(Program.entity_id.in_(update_ids)).delete(synchronize_session=False)

    # Apply updates in memory and add new variants/programs.
    for entity_data, existing, prev_data in to_update:
        existing.entity_type = entity_data["entity_type"]
        existing.primary_name = entity_data["primary_name"]
        existing.country = entity_data["country"]
        existing.date_of_birth = entity_data["date_of_birth"]
        existing.nationality = entity_data["nationality"]
        existing.date_listed = entity_data["date_listed"]
        existing.date_delisted = entity_data["date_delisted"]
        existing.last_updated = entity_data["last_updated"]
        existing.is_active = entity_data["is_active"]
        existing.remarks = entity_data.get("remarks")
        for variant in entity_data["name_variants"]:
            existing.name_variants.append(NameVariant(
                name=variant["name"],
                variant_type=variant["variant_type"],
                source=variant["source"],
            ))
        for program_name in entity_data["programs"]:
            existing.programs.append(Program(program_name=program_name))

    # Build new Entity objects for inserts.
    new_entities = []
    for entity_data in to_insert:
        entity = Entity(
            source_list=entity_data["source_list"],
            source_id=entity_data["source_id"],
            entity_type=entity_data["entity_type"],
            primary_name=entity_data["primary_name"],
            country=entity_data["country"],
            date_of_birth=entity_data["date_of_birth"],
            nationality=entity_data["nationality"],
            date_listed=entity_data["date_listed"],
            date_delisted=entity_data["date_delisted"],
            last_updated=entity_data["last_updated"],
            is_active=entity_data["is_active"],
            remarks=entity_data.get("remarks"),
        )
        for variant in entity_data["name_variants"]:
            entity.name_variants.append(NameVariant(
                name=variant["name"],
                variant_type=variant["variant_type"],
                source=variant["source"],
            ))
        for program_name in entity_data["programs"]:
            entity.programs.append(Program(program_name=program_name))
        new_entities.append((entity_data, entity))
        session.add(entity)

    # Flush inserts to get IDs, then build audit records.
    session.flush()
    print(f"  [{label}] Flushed inserts")

    for entity_data, entity in new_entities:
        inserted += 1
        pending_audits.append(EntityAudit(
            snapshot_id=snapshot.id,
            entity_id=entity.id,
            source_list=source_list,
            source_id=entity_data["source_id"],
            change_type="added",
            changed_at=now,
            primary_name=entity_data["primary_name"],
            previous_data=None,
            new_data=json.dumps(_scalar_fields(entity_data)),
        ))

    session.flush()
    print(f"  [{label}] Flushed updates")

    for entity_data, existing, prev_data in to_update:
        updated += 1
        pending_audits.append(EntityAudit(
            snapshot_id=snapshot.id,
            entity_id=existing.id,
            source_list=source_list,
            source_id=entity_data["source_id"],
            change_type="updated",
            changed_at=now,
            primary_name=entity_data["primary_name"],
            previous_data=json.dumps(prev_data),
            new_data=json.dumps(_scalar_fields(entity_data)),
        ))

    if pending_audits:
        session.add_all(pending_audits)
        session.flush()
        pending_audits.clear()

    # --- Removal detection ---
    # Use the already-loaded existing_map — no extra query needed.
    removed = 0
    for entity in existing_map.values():
        if entity.source_id not in seen_source_ids and entity.is_active:
            prev_data = _scalar_fields(entity)
            entity.is_active = False
            pending_audits.append(EntityAudit(
                snapshot_id=snapshot.id,
                entity_id=entity.id,
                source_list=source_list,
                source_id=entity.source_id,
                change_type="removed",
                changed_at=now,
                primary_name=entity.primary_name,
                previous_data=json.dumps(prev_data),
                new_data=None,
            ))
            removed += 1

    if pending_audits:
        session.add_all(pending_audits)
    session.flush()

    return inserted, updated, removed


# ---------------------------------------------------------------------------
# Per-source entry points
# ---------------------------------------------------------------------------

def _run_ingestion(source_list, label, download_fn, parse_fn, date_extractor=None):
    """
    Full ingestion pipeline for one source list:
      1. Download raw content.
      2. Hash it and compare against the last successful snapshot.
         → If unchanged, record an "unchanged" snapshot and stop.
      3. Archive the raw file to disk.
      4. Parse entities and upsert them into the DB.
      5. Detect removals (entities no longer in the list).
      6. Write a "success" snapshot with counts.
      On any exception, write an "error" snapshot and re-raise.
    """
    init_db()
    session = SessionLocal()
    now = datetime.now(timezone.utc)

    try:
        print(f"Downloading {label}...")
        raw_content = download_fn()

        content_hash = _compute_hash(raw_content)
        print(f"  Content hash: {content_hash[:16]}...")

        list_updated_at = None
        if date_extractor:
            try:
                import io as _io
                list_updated_at = _to_dd_mm_yyyy(date_extractor(
                    _io.BytesIO(raw_content) if isinstance(raw_content, bytes) else raw_content
                ))
                if list_updated_at:
                    print(f"  List updated at: {list_updated_at}")
            except Exception:
                pass

        last_ok = (
            session.query(ListSnapshot)
            .filter_by(source_list=source_list, status="success")
            .order_by(ListSnapshot.fetched_at.desc())
            .first()
        )

        if last_ok and last_ok.content_hash == content_hash:
            since = last_ok.fetched_at.date()
            print(f"{label}: list unchanged since {since}. Skipping ingestion.")
            session.add(ListSnapshot(
                source_list=source_list,
                fetched_at=now,
                content_hash=content_hash,
                list_updated_at=list_updated_at,
                status="unchanged",
            ))
            session.commit()
            return

        archive_path = _archive_raw(source_list, raw_content, now)
        print(f"  Archived to: {archive_path}")

        # Flush immediately so snapshot.id is available for audit FK references.
        snapshot = ListSnapshot(
            source_list=source_list,
            fetched_at=now,
            content_hash=content_hash,
            list_updated_at=list_updated_at,
            archive_path=archive_path,
            status="in_progress",
        )
        print(snapshot)
        session.add(snapshot)
        session.flush()

        print(f"Parsing {label} data...")
        entities = list(parse_fn(raw_content))
        print(f"Parsed {len(entities)} {label} entities")

        inserted, updated, removed = _load_entities(
            session, label, source_list, entities, snapshot
        )

        snapshot.record_count = len(entities)
        snapshot.inserted_count = inserted
        snapshot.updated_count = updated
        snapshot.removed_count = removed
        snapshot.status = "success"
        session.commit()

        print(f"\n{label} ingestion complete:")
        print(f"  Inserted: {inserted}")
        print(f"  Updated:  {updated}")
        print(f"  Removed:  {removed}")
        print(f"  Total:    {len(entities)}")

    except Exception as e:
        session.rollback()
        try:
            session.add(ListSnapshot(
                source_list=source_list,
                fetched_at=now,
                status="error",
                error_message=str(e)[:2000],
            ))
            session.commit()
        except Exception:
            pass
        print(f"Error during {label} ingestion: {e}")
        raise
    finally:
        session.close()


def load_fcdo():
    _run_ingestion("FCDO", "FCDO", download_fcdo, parse_fcdo, fcdo_list_date)


def load_ofac():
    _run_ingestion("OFAC_SDN", "OFAC", download_ofac, parse_ofac, ofac_list_date)


def load_eu():
    _run_ingestion("EU_CFSL", "EU", download_eu, parse_eu, eu_list_date)


def load_all():
    load_fcdo()
    load_ofac()
    load_eu()


if __name__ == "__main__":
    load_all()

import threading

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import text, func

from database import engine, Base, get_db
from models.sanctions import Entity, NameVariant, Program, ListSnapshot, EntityAudit

from rapidfuzz.distance import JaroWinkler
from rapidfuzz.fuzz import token_sort_ratio, token_set_ratio
from rapidfuzz.utils import default_process

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Sanctions Screening API",
    description="Screen names against OFAC, EU and UK FCDO sanctions lists",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


VALID_SOURCE_LISTS = {"OFAC_SDN", "EU_CFSL", "FCDO"}


class EntityDetail(BaseModel):
    entity_id: int
    primary_name: str
    entity_type: str
    source_list: str
    source_id: str
    is_active: bool
    country: Optional[str]
    date_of_birth: Optional[str]
    nationality: Optional[str]
    date_listed: Optional[str]
    date_delisted: Optional[str]
    last_updated: Optional[str]
    programs: list[str]
    name_variants: list[str]
    remarks: Optional[str]


class ScreenRequest(BaseModel):
    name: str
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    limit: int = Field(default=20, ge=1)
    source_lists: Optional[list[str]] = Field(default=None)


class BatchScreenRequest(BaseModel):
    names: list[str]
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    limit: int = Field(default=20, ge=1)
    source_lists: Optional[list[str]] = Field(default=None)


class BatchScreenResponse(BaseModel):
    name: str
    matches: list["ScreenResult"]


class ScreenResult(BaseModel):
    entity_id: int
    primary_name: str
    entity_type: str
    source_list: str
    source_id: str
    country: Optional[str]
    date_of_birth: Optional[str]
    nationality: Optional[str]
    date_listed: Optional[str]
    programs: list[str]
    match_score: float
    matched_variant: str
    matched_variant_type: str

class SanctionsListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[EntityDetail]


class EntityStatsResponse(BaseModel):
    total: int
    by_type: dict[str, int]
    by_regime: dict[str, int]


class HealthCheckResponse(BaseModel):
    status: str
    version: str
    database_connected: bool
    entry_counts: dict[str, int]


class SnapshotResponse(BaseModel):
    id: int
    source_list: str
    fetched_at: str
    content_hash: Optional[str]
    list_updated_at: Optional[str]
    archive_path: Optional[str]
    record_count: Optional[int]
    inserted_count: Optional[int]
    updated_count: Optional[int]
    removed_count: Optional[int]
    status: str
    error_message: Optional[str]


class SnapshotListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[SnapshotResponse]


class AuditResponse(BaseModel):
    id: int
    snapshot_id: int
    entity_id: Optional[int]
    source_list: str
    source_id: str
    change_type: str
    changed_at: str
    primary_name: Optional[str]
    previous_data: Optional[str]
    new_data: Optional[str]


class AuditListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[AuditResponse]


class IngestRequest(BaseModel):
    source_list: Optional[str] = Field(default=None, description="Source list to ingest. If omitted, all lists are ingested.")


class IngestResponse(BaseModel):
    message: str
    source_lists: list[str]





@app.get("/")
def root():
    return {"message": "Sanctions Screening API is running"}

@app.get("/health", response_model=HealthCheckResponse)
def health_check(db: Session = Depends(get_db)):
    # Check database connection
    try:
        db.execute(text("SELECT 1"))
        database_connected = True
    except Exception:
        database_connected = False

    # Get entry counts for each source list
    entry_counts = {}
    for source_list in VALID_SOURCE_LISTS:
        count = db.query(Entity).filter(Entity.source_list == source_list).count()
        entry_counts[source_list] = count


    return HealthCheckResponse(
        status="healthy" if database_connected else "unhealthy",
        version="0.1.0",
        database_connected=database_connected,
        entry_counts=entry_counts
    )

@app.get("/sanctions", response_model=SanctionsListResponse)
def get_sanctions(
    source_list: Optional[str] = None,
    entity_type: Optional[str] = None,
    country: Optional[str] = None,
    q: Optional[str] = None,
    is_active: bool = True,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    query = db.query(Entity)

    if source_list:
        query = query.filter(Entity.source_list == source_list)
    if entity_type:
        query = query.filter(Entity.entity_type == entity_type)
    if country:
        query = query.filter(Entity.country.ilike(f"%{country}%"))
    if q:
        query = query.filter(Entity.primary_name.ilike(f"%{q}%"))

    query = query.filter(Entity.is_active == is_active)

    total = query.count()
    entities = (
        query
        .options(selectinload(Entity.programs), selectinload(Entity.name_variants))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    results = [
        EntityDetail(
            entity_id=e.id,
            primary_name=e.primary_name,
            entity_type=e.entity_type,
            source_list=e.source_list,
            source_id=e.source_id,
            is_active=e.is_active,
            country=e.country,
            date_of_birth=e.date_of_birth,
            nationality=e.nationality,
            date_listed=e.date_listed,
            date_delisted=e.date_delisted,
            last_updated=e.last_updated,
            programs=[p.program_name for p in e.programs],
            name_variants=[v.name for v in e.name_variants],
            remarks=e.remarks,
        )
        for e in entities
    ]

    return SanctionsListResponse(total=total, page=page, page_size=page_size, results=results)


@app.get("/sanctions/stats", response_model=EntityStatsResponse)
def get_sanctions_stats(
    source_list: Optional[str] = None,
    is_active: bool = True,
    db: Session = Depends(get_db),
):
    if source_list is not None and source_list not in VALID_SOURCE_LISTS:
        raise HTTPException(status_code=400, detail=f"Invalid source_list. Must be one of: {sorted(VALID_SOURCE_LISTS)}")

    base_filter = [Entity.is_active == is_active]
    if source_list:
        base_filter.append(Entity.source_list == source_list)

    total = db.query(func.count(Entity.id)).filter(*base_filter).scalar()

    type_rows = (
        db.query(Entity.entity_type, func.count(Entity.id))
        .filter(*base_filter)
        .group_by(Entity.entity_type)
        .all()
    )

    regime_rows = (
        db.query(Program.program_name, func.count(Program.id))
        .join(Entity, Program.entity_id == Entity.id)
        .filter(*base_filter)
        .group_by(Program.program_name)
        .order_by(func.count(Program.id).desc())
        .all()
    )

    return EntityStatsResponse(
        total=total,
        by_type={row[0]: row[1] for row in type_rows},
        by_regime={row[0]: row[1] for row in regime_rows},
    )


@app.get("/sanctions/{source_list}/{source_id}", response_model=EntityDetail)
def get_sanctions_entry(source_list: str, source_id: str, db: Session = Depends(get_db)):
    if source_list not in VALID_SOURCE_LISTS:
        raise HTTPException(status_code=400, detail=f"Invalid source_list. Must be one of: {sorted(VALID_SOURCE_LISTS)}")
    entry = (
        db.query(Entity)
        .filter(Entity.source_list == source_list, Entity.source_id == source_id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return EntityDetail(
        entity_id=entry.id,
        primary_name=entry.primary_name,
        entity_type=entry.entity_type,
        source_list=entry.source_list,
        source_id=entry.source_id,
        is_active=entry.is_active,
        country=entry.country,
        date_of_birth=entry.date_of_birth,
        nationality=entry.nationality,
        date_listed=entry.date_listed,
        date_delisted=entry.date_delisted,
        last_updated=entry.last_updated,
        programs=[p.program_name for p in entry.programs],
        name_variants=[v.name for v in entry.name_variants],
        remarks=entry.remarks,
    )



@app.post("/screen", response_model=list[ScreenResult])
def screen(request: ScreenRequest, db: Session = Depends(get_db)):
    if request.source_lists is not None:
        invalid = set(request.source_lists) - VALID_SOURCE_LISTS
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid source_lists: {sorted(invalid)}. Must be from: {sorted(VALID_SOURCE_LISTS)}")

    query_processed = default_process(request.name)

    # Fetch name variants, optionally filtered by source list
    variant_query = db.query(NameVariant).join(Entity, NameVariant.entity_id == Entity.id)
    if request.source_lists:
        variant_query = variant_query.filter(Entity.source_list.in_(request.source_lists))
    variants = variant_query.all()

    # Score every variant; track best score per entity_id
    best: dict[int, dict] = {}

    for variant in variants:
        processed = default_process(variant.name)

        jw = JaroWinkler.normalized_similarity(query_processed, processed)
        tsr = token_sort_ratio(query_processed, processed, processor=None) / 100.0
        tsetr = token_set_ratio(query_processed, processed, processor=None) / 100.0
        
        # Adjust token_set_ratio by length similarity to reduce false positives on short names
        len_ratio = min(len(query_processed), len(processed)) / max(len(query_processed), len(processed)) if max(len(query_processed), len(processed)) > 0 else 1.0
        tsetr_adjusted = tsetr * len_ratio
        score = max(jw, tsr, tsetr_adjusted)

        if score < request.threshold:
            continue

        existing = best.get(variant.entity_id)
        if existing is None or score > existing["match_score"]:
            best[variant.entity_id] = {
                "match_score": score,
                "matched_variant": variant.name,
                "matched_variant_type": variant.variant_type,
            }

    if not best:
        return []

    # Fetch matching entities (preserving score order)
    entity_ids = list(best.keys())
    entities = (
        db.query(Entity)
        .filter(Entity.id.in_(entity_ids), Entity.is_active == True)
        .all()
    )

    results = []
    for entity in entities:
        info = best[entity.id]
        programs = [p.program_name for p in entity.programs]
        results.append(
            ScreenResult(
                entity_id=entity.id,
                primary_name=entity.primary_name,
                entity_type=entity.entity_type,
                source_list=entity.source_list,
                source_id=entity.source_id,
                country=entity.country,
                date_of_birth=entity.date_of_birth,
                nationality=entity.nationality,
                date_listed=entity.date_listed,
                programs=programs,
                match_score=round(info["match_score"], 4),
                matched_variant=info["matched_variant"],
                matched_variant_type=info["matched_variant_type"],
            )
        )

    results.sort(key=lambda r: r.match_score, reverse=True)
    return results[: request.limit]


@app.post("/screen/batch", response_model=list[BatchScreenResponse])
def screen_batch(request: BatchScreenRequest, db: Session = Depends(get_db)):
    if request.source_lists is not None:
        invalid = set(request.source_lists) - VALID_SOURCE_LISTS
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid source_lists: {sorted(invalid)}. Must be from: {sorted(VALID_SOURCE_LISTS)}")

    # Load variants once for the entire batch, optionally filtered by source list
    variant_query = db.query(NameVariant).join(Entity, NameVariant.entity_id == Entity.id)
    if request.source_lists:
        variant_query = variant_query.filter(Entity.source_list.in_(request.source_lists))
    variants = variant_query.all()

    results = []
    for name in request.names:
        query_processed = default_process(name)
        best: dict[int, dict] = {}

        for variant in variants:
            processed = default_process(variant.name)
            jw = JaroWinkler.normalized_similarity(query_processed, processed)
            tsr = token_sort_ratio(query_processed, processed, processor=None) / 100.0
            tsetr = token_set_ratio(query_processed, processed, processor=None) / 100.0
            len_ratio = min(len(query_processed), len(processed)) / max(len(query_processed), len(processed)) if max(len(query_processed), len(processed)) > 0 else 1.0
            tsetr_adjusted = tsetr * len_ratio
            score = max(jw, tsr, tsetr_adjusted)

            if score < request.threshold:
                continue

            existing = best.get(variant.entity_id)
            if existing is None or score > existing["match_score"]:
                best[variant.entity_id] = {
                    "match_score": score,
                    "matched_variant": variant.name,
                    "matched_variant_type": variant.variant_type,
                }

        if not best:
            results.append(BatchScreenResponse(name=name, matches=[]))
            continue

        entity_ids = list(best.keys())
        entities = (
            db.query(Entity)
            .options(selectinload(Entity.programs))
            .filter(Entity.id.in_(entity_ids), Entity.is_active == True)
            .all()
        )

        matches = []
        for entity in entities:
            info = best[entity.id]
            matches.append(ScreenResult(
                entity_id=entity.id,
                primary_name=entity.primary_name,
                entity_type=entity.entity_type,
                source_list=entity.source_list,
                source_id=entity.source_id,
                country=entity.country,
                date_of_birth=entity.date_of_birth,
                nationality=entity.nationality,
                date_listed=entity.date_listed,
                programs=[p.program_name for p in entity.programs],
                match_score=round(info["match_score"], 4),
                matched_variant=info["matched_variant"],
                matched_variant_type=info["matched_variant_type"],
            ))

        matches.sort(key=lambda r: r.match_score, reverse=True)
        results.append(BatchScreenResponse(name=name, matches=matches[:request.limit]))

    return results


# ---------------------------------------------------------------------------
# Snapshot / Audit endpoints
# ---------------------------------------------------------------------------

@app.get("/snapshots", response_model=SnapshotListResponse)
def get_snapshots(
    source_list: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(ListSnapshot).order_by(ListSnapshot.fetched_at.desc())

    if source_list:
        if source_list not in VALID_SOURCE_LISTS:
            raise HTTPException(status_code=400, detail=f"Invalid source_list. Must be one of: {sorted(VALID_SOURCE_LISTS)}")
        query = query.filter(ListSnapshot.source_list == source_list)
    if status:
        query = query.filter(ListSnapshot.status == status)

    total = query.count()
    snapshots = query.offset((page - 1) * page_size).limit(page_size).all()

    return SnapshotListResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[
            SnapshotResponse(
                id=s.id,
                source_list=s.source_list,
                fetched_at=str(s.fetched_at),
                content_hash=s.content_hash,
                list_updated_at=s.list_updated_at,
                archive_path=s.archive_path,
                record_count=s.record_count,
                inserted_count=s.inserted_count,
                updated_count=s.updated_count,
                removed_count=s.removed_count,
                status=s.status,
                error_message=s.error_message,
            )
            for s in snapshots
        ],
    )


@app.get("/snapshots/{snapshot_id}", response_model=SnapshotResponse)
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snapshot = db.query(ListSnapshot).filter(ListSnapshot.id == snapshot_id).first()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return SnapshotResponse(
        id=snapshot.id,
        source_list=snapshot.source_list,
        fetched_at=str(snapshot.fetched_at),
        content_hash=snapshot.content_hash,
        list_updated_at=snapshot.list_updated_at,
        archive_path=snapshot.archive_path,
        record_count=snapshot.record_count,
        inserted_count=snapshot.inserted_count,
        updated_count=snapshot.updated_count,
        removed_count=snapshot.removed_count,
        status=snapshot.status,
        error_message=snapshot.error_message,
    )


@app.get("/audit", response_model=AuditListResponse)
def get_audit(
    source_list: Optional[str] = None,
    change_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    entity_id: Optional[int] = None,
    snapshot_id: Optional[int] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    print(source_list, change_type, date_from, date_to, entity_id, snapshot_id, page, page_size)
    query = db.query(EntityAudit).order_by(EntityAudit.changed_at.desc())

    if source_list:
        if source_list not in VALID_SOURCE_LISTS:
            raise HTTPException(status_code=400, detail=f"Invalid source_list. Must be one of: {sorted(VALID_SOURCE_LISTS)}")
        query = query.filter(EntityAudit.source_list == source_list)

    if change_type:
        if change_type not in {"added", "updated", "removed"}:
            raise HTTPException(status_code=400, detail="change_type must be one of: added, updated, removed")
        query = query.filter(EntityAudit.change_type == change_type)

    if date_from:
        query = query.filter(EntityAudit.changed_at >= date_from)
    if date_to:
        query = query.filter(EntityAudit.changed_at <= date_to)

    if entity_id is not None:
        query = query.filter(EntityAudit.entity_id == entity_id)
    if snapshot_id is not None:
        query = query.filter(EntityAudit.snapshot_id == snapshot_id)

    total = query.count()
    audits = query.offset((page - 1) * page_size).limit(page_size).all()

    return AuditListResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[
            AuditResponse(
                id=a.id,
                snapshot_id=a.snapshot_id,
                entity_id=a.entity_id,
                source_list=a.source_list,
                source_id=a.source_id,
                change_type=a.change_type,
                changed_at=str(a.changed_at),
                primary_name=a.primary_name,
                previous_data=a.previous_data,
                new_data=a.new_data,
            )
            for a in audits
        ],
    )


# ---------------------------------------------------------------------------
# Ingestion endpoint
# ---------------------------------------------------------------------------

_ingest_lock = threading.Lock()


def _run_ingest(source_lists: list[str]):
    from ingestion.loader import load_ofac, load_eu, load_fcdo

    loaders = {
        "OFAC_SDN": load_ofac,
        "EU_CFSL": load_eu,
        "FCDO": load_fcdo,
    }
    for sl in source_lists:
        loaders[sl]()


@app.post("/ingest", response_model=IngestResponse)
def ingest(request: IngestRequest, background_tasks: BackgroundTasks):
    if request.source_list:
        if request.source_list not in VALID_SOURCE_LISTS:
            raise HTTPException(status_code=400, detail=f"Invalid source_list. Must be one of: {sorted(VALID_SOURCE_LISTS)}")
        source_lists = [request.source_list]
    else:
        source_lists = sorted(VALID_SOURCE_LISTS)

    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="An ingestion is already in progress")

    def run():
        try:
            _run_ingest(source_lists)
        finally:
            _ingest_lock.release()

    background_tasks.add_task(run)

    return IngestResponse(
        message="Ingestion started in the background",
        source_lists=source_lists,
    )

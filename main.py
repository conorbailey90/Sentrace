from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.orm import Session, selectinload

from database import engine, Base, get_db
from models.sanctions import Entity, NameVariant, Program

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


@app.get("/")
def root():
    return {"message": "Sanctions Screening API is running"}

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
    print(request.name)

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
        else:
            print(variant.name)
            print('JW: ', jw )
            print('TSR: ', tsr )
            print('TSETR (adjusted): ', tsetr_adjusted )
            print('')

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

    print(request.names)
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

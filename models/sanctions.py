from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Boolean, ForeignKey,
    UniqueConstraint, Index, DateTime, Text
)
from sqlalchemy.orm import relationship

from database import Base


class Entity(Base):
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_list = Column(String(20), nullable=False)
    source_id = Column(String(50), nullable=False)
    entity_type = Column(String(20), nullable=False)
    primary_name = Column(String(500), nullable=False)
    country = Column(String(200), nullable=True)
    date_of_birth = Column(String(100), nullable=True)
    nationality = Column(String(200), nullable=True)
    date_listed = Column(String(50), nullable=True)
    date_delisted = Column(String(50), nullable=True)
    last_updated = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    remarks = Column(Text, nullable=True)

    name_variants = relationship(
        "NameVariant", back_populates="entity", cascade="all, delete-orphan"
    )
    programs = relationship(
        "Program", back_populates="entity", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source_list", "source_id", name="uq_source_list_id"),
        Index("ix_entities_source_list", "source_list"),
        Index("ix_entities_entity_type", "entity_type"),
    )

    def __repr__(self):
        return f"<Entity {self.source_list}:{self.source_id} {self.primary_name!r}>"


class NameVariant(Base):
    __tablename__ = "name_variants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(500), nullable=False)
    variant_type = Column(String(50), nullable=False)
    source = Column(String(100), nullable=False)

    entity = relationship("Entity", back_populates="name_variants")

    __table_args__ = (
        Index("ix_name_variants_name", "name"),
        Index("ix_name_variants_entity_id", "entity_id"),
    )

    def __repr__(self):
        return f"<NameVariant {self.variant_type}: {self.name!r}>"


class Program(Base):
    __tablename__ = "programs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    program_name = Column(String(200), nullable=False)

    entity = relationship("Entity", back_populates="programs")

    __table_args__ = (
        Index("ix_programs_entity_id", "entity_id"),
    )

    def __repr__(self):
        return f"<Program {self.program_name!r}>"


class ListSnapshot(Base):
    """One row per fetch attempt for a given source list."""
    __tablename__ = "list_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_list = Column(String(20), nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    # SHA-256 hex digest of the raw downloaded content.
    # Identical hash = list has not changed since last successful run.
    content_hash = Column(String(64), nullable=True)
    list_updated_at = Column(String(100), nullable=True)
    archive_path = Column(String(500), nullable=True)
    record_count = Column(Integer, nullable=True)
    inserted_count = Column(Integer, nullable=True)
    updated_count = Column(Integer, nullable=True)
    removed_count = Column(Integer, nullable=True)
    # "success" | "unchanged" | "in_progress" | "error"
    status = Column(String(20), nullable=False)
    error_message = Column(Text, nullable=True)

    audits = relationship(
        "EntityAudit", back_populates="snapshot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_list_snapshots_source_list", "source_list"),
        Index("ix_list_snapshots_fetched_at", "fetched_at"),
    )

    def __repr__(self):
        return f"<ListSnapshot {self.source_list} {self.fetched_at} {self.status}>"


class EntityAudit(Base):
    """One row per entity change detected within a snapshot run."""
    __tablename__ = "entity_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey("list_snapshots.id"), nullable=False)
    # Nullable because the entity row may later be deleted.
    entity_id = Column(
        Integer, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    source_list = Column(String(20), nullable=False)
    source_id = Column(String(50), nullable=False)
    # "added" | "updated" | "removed"
    change_type = Column(String(20), nullable=False)
    changed_at = Column(DateTime, nullable=False)
    primary_name = Column(String(500), nullable=True)
    # JSON-serialised scalar fields of the entity before/after the change.
    # NULL for "added" (no previous state) and "removed" (no new state).
    previous_data = Column(Text, nullable=True)
    new_data = Column(Text, nullable=True)

    snapshot = relationship("ListSnapshot", back_populates="audits")

    __table_args__ = (
        Index("ix_entity_audit_snapshot_id", "snapshot_id"),
        Index("ix_entity_audit_entity_id", "entity_id"),
        Index("ix_entity_audit_change_type", "change_type"),
        Index("ix_entity_audit_source", "source_list", "source_id"),
    )
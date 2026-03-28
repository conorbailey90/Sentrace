from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    is_active = Column(Boolean, default=True )
    source_list = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)
    primary_name = Column(String, nullable=False, index=True)
    country = Column(String, nullable=True)
    date_of_birth = Column(String, nullable=True)
    nationality = Column(String, nullable=True)
    date_listed = Column(String, nullable=True)
    date_delisted = Column(String, nullable=True)
    last_updated = Column(String, nullable=True)

    aliases = relationship("ListingAlias", back_populates="listing", cascade="all, delete-orphan")
    programs = relationship("ListingProgram", back_populates="listing", cascade="all, delete-orphan")


class ListingAlias(Base):
    __tablename__ = "listing_aliases"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    alias_name = Column(String, nullable=False, index=True)
    alias_type = Column(String, nullable=True)

    listing = relationship("Listing", back_populates="aliases")


class ListingProgram(Base):
    __tablename__= "listing_programs"

    id = Column(Integer, primary_key=True, index =True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    program_name = Column(String, nullable=False)

    listing = relationship("Listing", back_populates="programs")
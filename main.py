from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine, Base
from models.models import Entity, EntityAlias

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Sanctions Screening API",
    description="Screen names against OFAC, OFUK and EU sanctions lists",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "Sanctions Screening API is running"}
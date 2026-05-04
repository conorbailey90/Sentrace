# Fractal — Sanctions Screening API

Architecture and design reference for the ingestion pipeline and history system.

---

## Project Structure

```
fractal/
├── main.py                     # FastAPI app entry point (stub — health check only)
├── database.py                 # SQLAlchemy engine, session, and init_db()
├── models/
│   └── sanctions.py            # All ORM table definitions
├── ingestion/
│   ├── loader.py               # Orchestrates download → hash → archive → parse → upsert
│   ├── fcdo_parser.py          # UK FCDO CSV parser
│   ├── ofac_parser.py          # US OFAC SDN Enhanced XML parser
│   ├── eu_parser.py            # EU Consolidated Financial Sanctions List XML parser
│   └── name_utils.py           # Shared company suffix-stripping utility
├── list-archive/               # Raw downloaded files, one per successful run (gitignored)
│   ├── FCDO/
│   ├── OFAC_SDN/
│   └── EU_CFSL/
├── sanctions.db                # SQLite database (runtime artefact)
└── ARCHITECTURE.md             # This file
```

Run the full ingestion pipeline from the project root:

```bash
PYTHONPATH=. python -m ingestion.loader
```

Run the API:

```bash
uvicorn main:app --reload
```

---

## Data Sources

| Source | Format | URL |
|--------|--------|-----|
| UK FCDO | CSV | `https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv` |
| US OFAC SDN | XML | `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ENHANCED.XML` |
| EU CFSL | XML | `https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content` |

The EU endpoint accepts an optional `EU_CFSL_TOKEN` environment variable for authenticated access.

---

## Database Schema

### Core tables

#### `entities`
One row per sanctioned person, organisation, vessel, or aircraft.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `source_list` | String | `OFAC_SDN`, `FCDO`, `EU_CFSL` |
| `source_id` | String | Original identifier from the source list |
| `entity_type` | String | `individual`, `organisation`, `vessel`, `aircraft` |
| `primary_name` | String | |
| `country` | String | Nullable |
| `date_of_birth` | String | Nullable, stored as raw string from source |
| `nationality` | String | Nullable |
| `date_listed` | String | Nullable |
| `date_delisted` | String | Nullable |
| `last_updated` | String | Nullable |
| `is_active` | Boolean | Set to `False` when entity disappears from a list |

Unique constraint on `(source_list, source_id)` — this is the upsert key.

#### `name_variants`
One row per searchable name form. Pre-exploded at ingest time to enable fast exact/prefix lookups without fuzzy matching at query time.

| Column | Type | Notes |
|--------|------|-------|
| `entity_id` | FK → entities | CASCADE DELETE |
| `name` | String | The string to match against |
| `variant_type` | String | `full_name`, `first_last`, `natural_order`, `natural_first_last`, `surname_only`, `partial_middle`, `alias`, `stripped_suffix` |
| `source` | String | `primary`, `alias (aka)`, etc. |

#### `programs`
One row per sanctions programme an entity belongs to.

| Column | Type | Notes |
|--------|------|-------|
| `entity_id` | FK → entities | CASCADE DELETE |
| `program_name` | String | |

---

### History tables

#### `list_snapshots`
One row per fetch attempt, regardless of outcome. Gives a full run log.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `source_list` | String | `OFAC_SDN`, `FCDO`, `EU_CFSL` |
| `fetched_at` | DateTime | UTC |
| `content_hash` | String(64) | SHA-256 hex digest of the raw file |
| `archive_path` | String | Path to the saved raw file on disk |
| `record_count` | Integer | Total entities parsed in this version |
| `inserted_count` | Integer | New entities added |
| `updated_count` | Integer | Entities whose scalar fields changed |
| `removed_count` | Integer | Entities marked inactive (absent from list) |
| `status` | String | `success`, `unchanged`, `in_progress`, `error` |
| `error_message` | Text | Nullable, populated on `error` |

#### `entity_audit`
One row per genuine entity change within a snapshot run. Only written when data actually changed — not for every entity that was processed.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `snapshot_id` | FK → list_snapshots | |
| `entity_id` | FK → entities | Nullable — entity row may be deleted later |
| `source_list` | String | Redundant copy for querying after entity deletion |
| `source_id` | String | Redundant copy for the same reason |
| `change_type` | String | `added`, `updated`, `removed` |
| `changed_at` | DateTime | UTC |
| `primary_name` | String | Copied for quick display |
| `previous_data` | Text (JSON) | Scalar fields before the change. NULL for `added`. |
| `new_data` | Text (JSON) | Scalar fields after the change. NULL for `removed`. |

---

## How the Hash Check Works

Every run, before any parsing happens:

```
1. Download the raw file (bytes)
        │
        ▼
2. Compute SHA-256 → fixed 64-character hex string e.g. "a3f9c2d1e4b7..."
        │
        ▼
3. Query list_snapshots for the most recent *successful* run of this source
        │
        ├── No prior snapshot → first ever run, process everything
        │
        └── Prior snapshot found
              │
              ├── Hashes MATCH → file is byte-for-byte identical.
              │   Write an "unchanged" snapshot row, stop early.
              │
              └── Hashes DIFFER → list has been updated.
                    Archive the file, parse, upsert, write audits.
```

SHA-256 is a one-way function that produces the same digest for the same bytes, every time, and a completely different digest if even a single byte changes. Comparing two 64-character strings is effectively instantaneous — you never need to load or diff the old raw file.

The `updated_count` and audit records only reflect genuine data changes:
- Entities present in both runs with no scalar field changes produce no audit row.
- Running the loader twice against the same content: the second run hits the hash check and writes zero audit records.

---

## Ingestion Pipeline (per source)

```
_run_ingestion()
  │
  ├── download_fn()                  # HTTP GET raw content
  ├── _compute_hash()                # SHA-256 hex digest
  ├── compare vs last snapshot hash
  │     └── unchanged → write snapshot(status="unchanged"), return
  │
  ├── _archive_raw()                 # Save to list-archive/<source>/<timestamp>.<ext>
  ├── write snapshot(status="in_progress"), flush → get snapshot.id
  │
  ├── parse_fn()                     # Parser returns list of entity dicts
  │
  ├── _load_entities()
  │     ├── upsert_entity() × N      # Insert or update, capture prev/new state
  │     ├── write EntityAudit rows   # Only for added / actually-changed / removed
  │     └── removal detection        # Active DB entities absent from this snapshot
  │                                  # → is_active=False + audit row
  │
  └── update snapshot(status="success", counts)
```

---

## Parser Contract

All three parsers yield entity dicts with the same shape, making the loader source-agnostic:

```python
{
    "source_list":    str,        # "OFAC_SDN" | "FCDO" | "EU_CFSL"
    "source_id":      str,
    "entity_type":    str,        # "individual" | "organisation" | "vessel" | "aircraft"
    "primary_name":   str,
    "country":        str | None,
    "date_of_birth":  str | None,
    "nationality":    str | None,
    "date_listed":    str | None,
    "date_delisted":  None,       # always None at ingest time
    "is_active":      True,
    "last_updated":   str | None,
    "programs":       [str, ...],
    "name_variants":  [{"name": str, "variant_type": str, "source": str}, ...]
}
```

### Latin-script filtering
Both OFAC and EU parsers explicitly filter to Latin-script names only. Non-Latin aliases are skipped entirely, keeping the search index clean for Latin-alphabet matching.

### Name variant generation
Each individual is exploded into multiple variant strings at ingest time:

| variant_type | Example |
|---|---|
| `full_name` | `Putin, Vladimir Vladimirovich` |
| `first_last` | `Putin, Vladimir` |
| `natural_order` | `Vladimir Vladimirovich Putin` |
| `natural_first_last` | `Vladimir Putin` |
| `surname_only` | `Putin` |
| `partial_middle` | per middle-name token |
| `stripped_suffix` | `Acme` (from `Acme Corp Ltd`) |

Deduplication is handled via a `seen_variants` set (lowercased) shared across all aliases of the same entity.

---

## Useful Queries

```sql
-- Run history for all sources
SELECT source_list, fetched_at, status, record_count,
       inserted_count, updated_count, removed_count
FROM list_snapshots
ORDER BY fetched_at DESC;

-- Full change history for one entity
SELECT ea.change_type, ea.changed_at, ea.previous_data, ea.new_data
FROM entity_audit ea
JOIN entities e ON e.id = ea.entity_id
WHERE e.source_list = 'OFAC_SDN' AND e.source_id = '12345'
ORDER BY ea.changed_at;

-- Everything added/changed/removed in a specific run
SELECT change_type, primary_name, source_id
FROM entity_audit
WHERE snapshot_id = 7
ORDER BY change_type, primary_name;

-- All entities removed from FCDO list over time
SELECT ea.primary_name, ea.source_id, ea.changed_at
FROM entity_audit ea
WHERE ea.source_list = 'FCDO' AND ea.change_type = 'removed'
ORDER BY ea.changed_at DESC;
```

---

## Raw Archive

Every time a list changes, the raw file is saved to:

```
list-archive/<source_list>/<YYYY-MM-DDTHH-MM-SS>.<ext>
```

This gives point-in-time recovery — any historical version can be re-parsed by calling the relevant `parse_*` function directly against the archived file.

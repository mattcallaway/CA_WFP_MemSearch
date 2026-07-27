# California Working Families Party MemSearch Roster Application (Stage 1)

This is a secure, transaction-first, private web application designed to turn California Secretary of State (SOS) contribution records into an accurate, searchable membership roster.

---

## Stage 1 Features

- **Ingestion & Immature Records**: Imports SOS CSV records, checking file-level hashes, byte-for-byte row content hashes, and composite duplicate keys. Preserves raw records on rollback.
- **Identity Resolution**: DECUPLES exact Name + ZIP matches. Contributions are grouped into `ContributionCluster` records with low/medium/high confidence. Only verified clusters belonging to a verified `ContributorEntity` can yield authoritative member records.
- **Recurrence & Membership**: Calculates profile-level recurrence sequences (e.g. 20-40 day intervals, skip payments) and entity-level membership status (e.g. Active, Provisional, Lapsed).
- **Dataset Coverage Check**: Prevents false "Lapsed" or "One-Time" statuses when the imported dataset is stale or incomplete, substituting provisional status descriptions.
- **Non-destructive Rollback & Restore**: Restores pre-merge contribution cluster assignments when rollbacks are executed. Supports split-and-merge reversibility without deleting original records.
- **Secure Web UI**: Secure authentication, bulma-based premium metrics dashboard, column mapping previews, global search directory, and profile timeline views. Served 100% locally with zero CDN external calls.

---

## Stage 2A & 2A.1 Geography Features

- **Bounded Chunk Ingestion**: Geographic crosswalk files are processed in chunks of 500 rows. Leveraging database bulk inserts and chunk-level relationship pre-checks reduces SQL ingestion queries by over 97%.
- **Phase-by-Phase Query Profiling**: Integrates a lightweight `QueryProfiler` context wrapper to log query counts per phase without memory-intensive query logs.
- **Hardened ZIP Normalization**: Implements conservative normalization where 4-digit ZIP inputs are left unpadded (triggering `POSSIBLE_TRUNCATED_LEADING_ZERO` validation flags) unless explicitly authorized by the frozen mapping snapshot.
- **Range-constrained Weight Normalization**: Supports fractional and percentage weight normalizations with a strict database check constraint validating normalized values in range `[0.0, 1.0]`.
- **Decoupled Activation & Pending Proposals**: Dataset activation commits independently, logs audit events, and registers a pending resolution proposal.
- **Scoped Resolver Engine**: Grouping locations into 500-unit chunks, the resolver loads candidate places, postal areas, aliases, and associations matching only that chunk's keys.
- **Cache Rebuild CLI Command**: Reconstructs Location cached geographic attributes from authoritative current resolutions. Clears stale cache values when no resolution exists and reports multiple-current-resolution corruption.
- **PII Data Leak Protection**: Applies database-level `.values()` projections for geographers without roster permissions to prevent loading or hydrating sensitive personal data.

---

## Directory Structure

```
WFP MemSearch/
├── wfp_memsearch/          # Django Configuration
│   ├── settings.py
│   └── urls.py
├── roster/                 # Main Application
│   ├── static/
│   │   └── vendor/         # Vendored local CSS & JS
│   │       ├── css/
│   │       │   └── bulma.min.css
│   │       └── js/
│   │           └── htmx.min.js
│   ├── templates/          # HTML templates
│   ├── services/           # Decoupled business logic
│   │   ├── importer.py     # Ingestion & rollbacks
│   │   ├── resolver.py     # Clustering & parsing
│   │   └── membership.py   # Pattern & membership
│   ├── tests/              # Automated unit tests
│   │   ├── fixtures/
│   │   │   └── synthetic_contributions.csv
│   │   ├── test_importer.py
│   │   ├── test_resolver.py
│   │   └── test_membership.py
│   └── models.py           # Django model definitions
├── manage.py
├── requirements.txt
└── README.md
```

---

## Setup & Running Locally

### 1. Initialize Virtual Environment & Install Requirements
```bash
# Create venv
python -m venv venv

# Activate venv (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 2. Apply Migrations & Initialize Schema
```bash
python manage.py migrate
```

### 3. Create Admin Superuser
To log into the roster dashboard, you must create a secure administrative account:
```bash
python manage.py createsuperuser
```
Follow the prompt instructions to configure your username and password.

### 4. Run Development Server
```bash
python manage.py runserver
```
Navigate to [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your web browser.

---

## Running Automated Tests

To run the complete test suite (testing resolver name parsing, duplicate checking, rollbacks, and coverage limits):
```bash
python manage.py test
```

---

## Secure Purging Command (Exceptional deletion)

To permanently delete an import batch and all cascades (use only under administrator supervision via CLI):
```bash
python manage.py shell -c "from roster.services.importer import purge_batch; purge_batch(<batch_id>, actor='ADMIN')"
```
This physically purges RawContribution, Contribution, locations, and batch records. Normal web-based rollbacks are non-destructive.

---

## Rebuild Geography Cache Command

To rebuild cached geographic fields on `Location` records from authoritative current resolutions:
```bash
python manage.py rebuild_location_geography_cache [--dry-run] [--actor <username>] [--location-ids <id1> <id2> ...] [--run-id <id>]
```
Options:
- `--dry-run`: Preview updates without committing changes to the database.
- `--actor`: Log a custom actor name in the audit event.
- `--location-ids`: Limit the rebuild to specific location IDs.
- `--run-id`: Limit the rebuild to locations resolved in a specific resolution run.

Stale locations with no current resolution will have their cache cleared, and any locations with multiple current resolutions (indicating database corruption) will be reported and skipped without changes.


# California Working Families Party MemSearch Roster Application

This is a secure, transaction-first, private web application designed to turn California Secretary of State (SOS) contribution records into an accurate, searchable membership roster with geographic chapter assignments.

---

## Stage 1 Features

- **Ingestion & Immature Records**: Imports SOS CSV records, checking file-level hashes, byte-for-byte row content hashes, and composite duplicate keys. Preserves raw records on rollback.
- **Identity Resolution**: DECOUPLES exact Name + ZIP matches. Contributions are grouped into `ContributionCluster` records with low/medium/high confidence. Only verified clusters belonging to a verified `ContributorEntity` can yield authoritative member records.
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

## Stage 2B Chapter Definitions & Assignment Engine

- **Chapter Model & Rules**: Multi-target rules (`COUNTY`, `PLACE`, `POSTAL_AREA`, `INDIVIDUAL_OVERRIDE`) supporting `INCLUDE` and `EXCLUDE` modes.
- **Generation-Based Evaluation Runs**: Atomic assignment generations (`ChapterEvaluationRun`) in `PREVIEW` or `APPLY` mode. The previous evaluation run remains authoritative throughout evaluation and after any failure.
- **Conditional Unique Constraints**: Rules enforce unique target assignments based on type (`county_id`, `place_id`, `postal_area_id`, `contributor_entity_id`).
- **Overlaps Aggregation**: Detects contributors matching multiple chapters simultaneously without silent duplication.
- **Manual Overrides**: Documented inclusion/exclusion overrides with strict audit logging.

---

## Stage 2B.1 Identity & Import Integrity Repair (Final Closeout)

- **Single Source of Verification Truth**: Database `CheckConstraint` `check_is_verified_sync` on `ContributorEntity` requiring `is_verified=True` <-> `verification_status='VERIFIED'` and `is_verified=False` <-> `verification_status='UNVERIFIED'`.
- **Centralized Verification Services (`roster/services/identity.py`)**: `verify_contributor_identity`, `unverify_contributor_identity`, and `bulk_unverify_identity_drift` provide strict actor validation, `manage_identity` permission checks, method-specific evidence/explanation validations, atomic transactions, and non-PII audit logging.
- **Method-Specific Validations**: `ADMIN_REVIEW` requires explanation + actor; `EXTERNAL_IDENTITY_MATCH` requires structured evidence reference + explanation (NO raw source PII in evidence); `LEGACY_REVIEWED` requires legacy basis explanation.
- **Immutable Unique-Directory Manifests**: Executed repair manifests stored under `artifacts/audit/identity_repair/<uuid>/` containing `correction_manifest.json`, `rollback_manifest.json`, and `run_summary.json` with SHA-256 hashes. Reconstructed manifest generated for original 471-entity repair.
- **Authorized Rollback Command**: `python manage.py rollback_identity_drift_repair --manifest <path> --actor <username> --dry-run` supports SHA-256 verified rollback and reapplication.
- **File-Backed Multi-Thread Concurrency Testing**: `DuplicateUploadConcurrencyTestCase` verifies simultaneous duplicate file uploads and batch transitions cause 0 HTTP 500 / `IntegrityError` exceptions.
- **Query-Count Bounded Import Benchmarks**: `ImportBenchmarkTestCase` proves import query counts scale with chunk size (under 50 queries for 100 rows, under 120 queries for 1,000 rows).
- **Chapter Propagation & Staleness**: `ChapterPropagationTestCase` verifies identity unverification triggers `ENTITY_REEVALUATION` staleness and replacement generation without modifying historical chapter evaluation runs.
- **Provenance-Based Fixture Audit & Cleanup Commands**: `audit_synthetic_fixtures` and manifest-driven `cleanup_synthetic_fixtures` allow authorized, dry-run previewed cleanup of benchmark entities.
- **Retrieval-Level Privacy Sentinels**: Privacy sentinel suite asserts HTTP 403 for unauthorized requests lacking `view_sensitive_roster` and verifies `.values()` projections for aggregate routes.

---

## Directory Structure

```
WFP MemSearch/
├── wfp_memsearch/          # Django Configuration
│   ├── settings.py
│   └── urls.py
├── roster/                 # Main Application
│   ├── static/
│   ├── templates/          # HTML templates
│   ├── services/           # Decoupled business logic
│   │   ├── importer.py     # Ingestion & rollbacks
│   │   ├── resolver.py     # Clustering & parsing
│   │   ├── membership.py   # Pattern & membership
│   │   ├── identity.py     # Centralized identity verification
│   │   └── chapter_engine.py # Chapter assignment engine
│   ├── management/
│   │   └── commands/       # CLI commands
│   │       ├── repair_recurrence_identity_drift.py
│   │       ├── reconstruct_executed_repair_manifest.py
│   │       ├── rollback_identity_drift_repair.py
│   │       ├── audit_synthetic_fixtures.py
│   │       └── cleanup_synthetic_fixtures.py
│   ├── tests/              # Automated unit test suite (82 tests)
│   └── models.py           # Django model definitions
├── manage.py
├── requirements.txt
└── README.md
```

---

## Setup & Running Locally

```bash
# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Apply migrations
python manage.py migrate

# Create admin user & setup roles
python manage.py setup_roles
python manage.py createsuperuser

# Run server
python manage.py runserver
```

---

## Running Automated Tests & Management Commands

```bash
# Run full unit test suite (82 tests)
python manage.py test

# Identity Repair Commands
python manage.py repair_recurrence_identity_drift --dry-run --actor admin
python manage.py reconstruct_executed_repair_manifest
python manage.py rollback_identity_drift_repair --manifest artifacts/audit/identity_repair/reconstructed_04be683e-5749-8c93-a6b1-dbf1a4a8b02f --actor admin --dry-run

# Synthetic Fixture Audit & Cleanup Commands
python manage.py audit_synthetic_fixtures
python manage.py cleanup_synthetic_fixtures --manifest artifacts/audit/synthetic_cleanup_manifest.json --actor admin --dry-run
```

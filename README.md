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

## Stage 2B.1 Identity and Import Integrity Repair

- **Decoupled Identity Verification**: Transaction recurrence and cluster confidence describe cluster cohesion and payment frequency. They NEVER automatically verify entity identity or promote unverified donors to authoritative active membership.
- **Explicit Verification Provenance**: Adds `verification_status` (`UNVERIFIED`, `VERIFIED`), `verification_method` (`NONE`, `ADMIN_REVIEW`, `EXTERNAL_IDENTITY_MATCH`, `LEGACY_REVIEWED`), `verified_at`, `verified_by`, and `verification_evidence` fields with database check constraints.
- **Provisional Recurrence Pattern Authority**: Unverified recurring donors receive `PROVISIONAL` membership status with `membership_authority = 'PROVISIONAL'` and `recurrence_pattern_status = 'RECURRING_PATTERN'`, preventing false claims of authoritative membership.
- **1-to-1 Current Assessment Uniqueness**: `MembershipAssessment` enforces `unique_current_assessment_per_entity` DB constraint (`is_current=True`). Superseded historical assessments are retained with `is_current=False`.
- **Reversible Repair Command**: `python manage.py repair_recurrence_identity_drift --dry-run` identifies and repairs entities auto-verified solely through cluster-confidence escalation.
- **Canonical ImportBatch Constraint**: Restored `unique_completed_batch_file_hash` database constraint on `ImportBatch`. Duplicate file uploads create `ImportAttempt` audit records without altering `COMPLETED` batch lifecycles.

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
│   │   ├── membership.py   # Pattern & membership
│   │   └── chapter_engine.py # Chapter assignment engine
│   ├── management/
│   │   └── commands/       # CLI commands
│   │       └── repair_recurrence_identity_drift.py
│   ├── tests/              # Automated unit tests
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
To log into the roster dashboard, create an administrative account:
```bash
python manage.py createsuperuser
```

### 4. Run Development Server
```bash
python manage.py runserver
```
Navigate to [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your web browser.

---

## Running Automated Tests

To run the complete test suite:
```bash
python manage.py test
```

---

## Identity Drift Repair Command

To preview or run the identity drift repair:
```bash
python manage.py repair_recurrence_identity_drift --dry-run --actor <username>
python manage.py repair_recurrence_identity_drift --confirm --actor <username>
```

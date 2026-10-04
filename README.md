# Avanyam

**Internal Learning & Capability Building Platform**

Avanyam is a web-based Learning Management System (LMS) and competency-mapping
platform for a single organization, deployed on the internal network only. It
replaces spreadsheet-and-email-driven training coordination with an auditable
system.

This repository implements the **CAPACITY CONNECT** brief (Digital Capacity
Building and Learning Management Portal). `avanyam_intro.txt` is the
authoritative specification; where it conflicts with the management-level
`avanyam_brief.txt`, the engineering brief wins.

---

## What makes it more than a generic LMS

Three capabilities sit at the core of the design:

1. **Competency mapping.** A framework maps `SUBJECT → required COMPETENCY →
   TRAINER`, with evidence-backed, admin-verified proficiency claims. A pure
   scoring function ranks eligible trainers per subject and surfaces verified
   gaps. Every recommendation persists an explainable score breakdown, so there
   is no black box.

2. **Compliance-grade assessment.** The question paper is snapshotted immutably
   at attempt start; correct answers are never sent to the client; timing is
   server-authoritative only; option order is shuffled per attempt; submits are
   idempotent; every transition is audit-logged. Later edits to a question can
   never corrupt an in-flight or historical attempt.

3. **Signup without uncontrolled access.** Registration is open to staff, but an
   account is inert until an administrator approves it. Identity (corporate
   OIDC/LDAP, where available) is strictly separated from authorization
   (PostgreSQL). Neither directory membership nor signup can self-activate a
   role — approval is always explicit.

### Roles

| Role | Scope |
|---|---|
| **Trainee** | Profile, course enrollment, learning content, MCQ assessments, feedback |
| **Trainer** | Profile, questionnaire authoring, own-content library, cohort monitoring |
| **Admin** | User approval, role management, dashboards, homepage feed publishing |

Multi-role per user is supported: a senior engineer who also trains holds
`TRAINEE + TRAINER`, with a session-scoped active-role switcher.

---

## Three-VM structure

Avanyam is deployed across three virtual machines, one per `avanyam_*` directory
in this repository. The split is by **failure domain**: isolating data and media
means an OOM or a bad deploy on the application tier cannot take the database or
the object store with it.

| VM | Directory | Role | Hardware | Runs the app? |
|---|---|---|---|---|
| **VM1** | `avanyam_terra/` | **Application** | 4 vCPU · 8 GB · 100 GB SSD | Yes — Gunicorn + Celery `fast` |
| **VM2** | `avanyam_aero/` | **Data** | 4 vCPU · 16 GB · 300 GB SSD | **No — Django must not be installed** |
| **VM3** | `avanyam_aqua/` | **Media and Compute** | 8 vCPU · 16 GB · 1 TB SSD | Yes — Celery `bulk` |

> **The backend is not inside any `avanyam_*/` directory.** It lives at the
> repository root (`manage.py`, `src/`) and is deployed to VM1 and VM3 as the
> *same* image — only the environment variables differ. The `avanyam_*/`
> directories hold per-host **deployment roots**: virtualenv, `.env`, logs, and
> host config. They are deliberately not code, because VM1 and VM3 run the same
> code and must never diverge.
>
> A "monolith" here means one codebase, one database, one artifact — not one
> machine. What is split across three VMs is the *processes*, not the
> application.

### VM1 — `avanyam_terra` · Application

Nginx `:443` (TLS termination, security headers, static assets),
Gunicorn/Django, Celery `fast` workers, Redis 7. Serves everything users
touch. **Stateless**, so it can be replaced or restarted without losing
anything. The only
internet-adjacent surface.

Nginx is configured to serve protected media itself via `X-Accel-Redirect`
(`avanyam_terra/conf/nginx/avanyam.conf:150-153`): Django makes the
authorization decision and returns a token, and the `internal` location serves
the bytes, so objects are never publicly addressable.

**This path is not yet wired to the object store** — the location aliases a
filesystem path while `STORAGES["default"]` in
`src/config/settings/base.py` points at S3. It becomes correct once uploads are
implemented (P2), where the choice is between an S3 presigned URL and a local
Nginx cache.

### VM2 — `avanyam_aero` · Data

PostgreSQL 16 primary with a streaming standby, PgBouncer, WAL-G shipping
offsite, and Keycloak 26.x as the identity provider. **Nothing here is
internet-facing.** Holds everything of value.

Separate databases, not one — created by `avanyam_aero/bootstrap.sql`, each
with a distinct owner role:

| Database | Owner role | Django alias | Notes |
|---|---|---|---|
| `avanyam` | `avanyam_migrate` | `default` | The runtime `avanyam_app` role holds this schema but owns no DDL |
| `avanyam_audit` | `avanyam_audit` | `audit` | Append-only trail. `avanyam_app` gets `CONNECT`, never `CONNECT`+write on tables |
| `avanyam_reporting` | `avanyam_reporting` | `reporting` | Reporting reads. Routed via `config.router.AuditAndReportingRouter` |
| `keycloak` | `keycloak` | — | Migrated by `kc.sh` at startup, never by Django's `migrate` |
| `keycloak_test` | `keycloak_test` | — | Realm fixtures for CI. Created only when `:create_ci_db` is set; never on production |

Runtime, migrate, audit and reporting are four separate login roles. A
compromised web process holds `avanyam_app`, which is granted only
`SELECT, INSERT, UPDATE, DELETE` — it cannot `ALTER` the schema. That is why
`migrate` has its own role and its own `CONN_MAX_AGE = 0`.

PgBouncer's transaction pool is safe for Django but **not** for Keycloak, which
breaks prepared-statement and session state and presents as sporadic login
failures. Keycloak therefore gets its own connection pool outside that pool.

The 16 GB is deliberate — Postgres keeps hot data in `shared_buffers` plus OS
page cache, and the standby holds its own connection set. The 300 GB grows with
the append-only audit log and per-attempt snapshots, which never shrink.

### VM3 — `avanyam_aqua` · Media and Compute

SeaweedFS (S3-compatible object storage), ClamAV (antivirus scanning), FFmpeg
video transcoding, and Celery `bulk` workers. Handles video, separated so a long
transcode cannot starve the app.

The 8 vCPU is for FFmpeg: one 30-minute transcode saturates every core, so
transcodes are concurrency-capped at 2 rather than 4 — otherwise a single long
video would OOM the box and take ClamAV down with it.

SeaweedFS is a deliberate substitution for the MinIO named in the engineering
brief. It keeps the S3 API, so the swap is a `.env` change, not a code change.

> **Not yet wired.** The `fast` / `bulk` queue split is specified and the systemd
> units pass `-Q fast`, but no `task_queues` or `task_routes` exist in
> `config/celery.py` yet, so both workers currently consume the default `celery`
> queue. Routing arrives with the video pipeline (P3), not before.

### Sizing basis

The brief sizes these hosts without stating a user or concurrency target, and
the hardware figures above are the brief's, not derived from measured load. A two-VM
launch by merging VM1 and VM3 is viable only at small scale: the shared cores
would let a transcode starve page requests, so video work must come off the app
box or transcode concurrency must drop to 1.

> **Development ≠ production.** All three VMs are simulated on a single ~7 GB /
> 4 vCPU machine, so nothing above is provisioned as written.

---

## Architecture

**Modular monolith:** one deployable, one database, Django apps as bounded
contexts. Justified by single-org scale, a small team, and the transactional
consistency required across enrollment → progress → certificate. Microservices
would multiply operational surface for no benefit. The seams for future
extraction — video transcoding, report aggregation, notification fan-out — are
already isolated as separate processes.

Each app is layered:

| Layer | Responsibility |
|---|---|
| `api/` | Thin DRF controllers. No business logic, no QuerySets |
| `service/` | Orchestration and transaction boundaries |
| `domain/` | **Pure logic.** No ORM, no request, no settings |
| `repositories/` | The only layer that writes QuerySets |
| `policies.py` | Pure `can(user, action, obj)` authorization, exhaustively tested |

A small typed event bus decouples assessment → certification → announcements
without circular imports.

### Stack

Python 3.11 · Django 5.2 LTS · PostgreSQL 16 · Redis 7 · Celery · Nginx ·
Gunicorn · Keycloak (OIDC) · SeaweedFS · ClamAV · FFmpeg · structlog ·
OpenTelemetry · Prometheus · uv

Security tooling declared in `pyproject.toml`: `bandit` (SAST), `pip-audit`
(dependency CVEs), `django-axes` (lockout), `django-csp` (nonce-based CSP),
Argon2id password hashing, `whitenoise` (static fallback without Nginx).

Semgrep and Trivy are specified in the brief but **not yet installed or wired to
CI** — there is no `.github/` directory. `uv.lock` pins all Python dependencies;
the Docker image pins SeaweedFS by digest, not tag.

---

## Repository layout

```
manage.py                  Django entry point
pyproject.toml             Dependencies, tooling and quality-gate config
avanyam_*.txt              Authoritative spec and management brief
*.md                       Project documentation (gitignored)
src/
  apps/
    accounts/              Identity: signup, approval, roles, undo
    common/                Shared models and logging
    pages/                 Home page
  config/                  Settings, logging, health, observability
  frontend/
    templates/             Server-rendered HTML
    static/                CSS, JS, images
avanyam_terra/             VM1 deployment root — config only, no application code
avanyam_aero/              VM2 deployment root — config only, Django-free by design
avanyam_aqua/              VM3 deployment root — config only, no application code
avanyam_intro.txt          Authoritative engineering specification
avanyam_brief.txt          Management proposal
prd.md                     Product requirements
architecture.md            System architecture
rules.md                   Engineering rules
design.md                  Design system
tasks.md                   Per-task project status
memory.md                  Architectural decision log
DEPLOYMENT.md              Deployment procedure
CREDENTIALS.md             Credential runbook (local only, never committed)
```

Compose files live with the host they deploy, not in a shared directory —
`depends_on` does not cross hosts. `avanyam_aqua/docker-compose.yml` is the only
one present so far, and it containerises the object store alone; `clamd`, `ffmpeg`
and the Celery workers run as host services.

### Where the backend actually is

```bash
$ git ls-files avanyam_terra/ | wc -l
9# none are .py -- config only

$ python manage.py shell -c "from django.apps import apps; print(apps.get_app_config('accounts').path)"
/home/rupesh/avanyam_lms/src/apps/accounts
```

`manage.py` inserts `src/` onto `sys.path` itself (lines 21-23), so the package
is importable as `config.*` and `apps.*` without installation. Django settings
are `config.settings.{dev,staging,prod,test}`.

`avanyam_terra/` tracks nine files, none of them Python: `.env.example`, `conf/`
(env, nginx, redis), `scripts/gen-dev-tls.sh`, and four `systemd/*.service`
units. Its Python is a virtualenv, not a package.

---

## Development setup

The `uv`-managed virtualenv lives at
`avanyam_terra/.avanyam_terra_venv/`. It is referenced by relative path below.

```bash
# Activate
source avanyam_terra/.avanyam_terra_venv/bin/activate

# Run the test suite
python -m pytest -q

# Django management commands
python manage.py check
python manage.py migrate
python manage.py makemigrations --check --dry-run
```

416 tests currently collect and pass.

### Quality gates

Run these before every commit:

```bash
python -m pytest -q                              # tests
python manage.py check                           # Django system checks
python manage.py makemigrations --check --dry-run  # no model/migration drift
ruff check src                                    # lint
```

### Test database

`pytest` uses `--reuse-db` against the **existing development database** because
no role on the development host has `CREATEDB`. Django therefore cannot create a
`test_avanyam` database. This has two consequences for new tests:

- Do not assume the Admin table is empty, or that the set of Admin notification
  recipients is the one your test created.
- Prefer membership assertions (`x in recipients`) over equality. Real
  development data is present and shared.
- **Never use `TransactionTestCase`**, and never let a test commit for real. The
  plain `db` fixture wraps each test in a transaction and rolls it back;
  `TransactionTestCase` truncates tables afterwards and **will destroy the
  seeded development accounts**. Switch to a real test database first — which means
  granting `CREATEDB`.
- The suite runs as the *migrate* role, not `avanyam_app`, so it does not
  exercise the runtime role's privileges.
  `src/apps/accounts/tests/test_db_privileges.py` covers that separately, by
  connecting as `avanyam_app`.

Migrations still run on every session, so a stale schema cannot hide a failure.

### Known deployment gaps

Recorded here so they are not mistaken for working infrastructure:

| Gap | Detail |
|---|---|
| Systemd units reference a stale layout | `avanyam_gunicorn.service` points at `avanyam.settings.production` / `avanyam.wsgi`; the real modules are `config.settings.prod` / `config.wsgi`. **The unit cannot start as written.** |
| `scripts/check-app.sh` probes wrong paths | Line 62 checks `avanyam_terra/manage.py` and `avanyam_terra/avanyam_terra`, neither of which exists |
| Celery `fast`/`bulk` routing undefined | No `task_queues` or `task_routes` in `config/celery.py`; both workers consume the default `celery` queue |
| `X-Accel-Redirect` not wired to storage | The Nginx `internal` location aliases a filesystem path while `STORAGES["default"]` is S3 |
| No `MEDIA_ROOT` | `base.py` sets `MEDIA_URL` only. Correct for S3, but uploads (P2) must decide between presigned URLs and a local cache |
| No CI | No `.github/`. Semgrep and Trivy are specified but not installed |

---

## Deployment notes

- **One compose file per host.** `depends_on` does not cross hosts, so a single
  giant compose file silently misleads.
- **Pin base images by digest**, not by tag. A `:latest` rebuild on a Tuesday can
  break a Friday deploy.
- **Run migrations as a one-shot container** with the migrate role, before the app
  rolls. Never inside an entrypoint script.
- **Secrets via mounted SOPS/age-encrypted files or Docker secrets** — never in a
  committed compose file's `environment:` block.
- **Resource limits on every container**, especially the transcoder.

Environment files are per-host under each `avanyam_*/conf/` directory —
`env.sh` plus host-specific config (nginx, pgbouncer, postgresql, keycloak).
They are never committed.

---

## Project status

Twelve phases, `P0`–`P11`. A pilot spans `P0`–`P6` (~8 engineer-months); full
scope is ~14–18 engineer-months.

| Phase | Scope | State |
|---|---|---|
| `P0` | Foundation | done |
| `P1` | Identity — signup, approval, roles | **in progress** — signup, approval, roles and the Admin-only queue are built and tested; OIDC/LDAP backends not started |
| `P2` | People — profiles, qualifications, uploads | not started |
| `P3` | Catalog and delivery | not started |
| `P4` | Enrollment | not started |
| `P5` | Assessment | not started |
| `P6` | Certification | not started |

`P7`–`P11` cover competency, reporting and hardening and are likewise unstarted.

`tasks.md` carries per-task status. `memory.md` records architectural decisions
(D-numbers) as they are made, including the reasoning and what would invalidate
each one.

### Open questions

Several items require client input before the identity and reporting work can
proceed: directory availability (LDAP/AD vs. Entra ID), the MFA mandate, and the
trainer visibility policy.

---

## Out of scope for v1

Signup that bypasses approval · billing · SCORM/xAPI authoring · DRM or
encrypted video · chat threads · native mobile apps (responsive PWA is the
target).

---

## License

Internal project. All rights reserved.
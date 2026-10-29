# FDR Streamline

A locally runnable Security Deposit/FDR Management Portal for **SIH Problem Statement 26265**, AICTE Regulation Bureau. The source requirements are in [ps.md](ps.md).

The portal keeps institution submissions, administrative decisions, date validity, independent bank evidence, and external refund execution distinct. It includes persistent records, private encrypted documents, review and correction workflows, renewals, release authorization, reminders, reports, CSV import/export, and audit history.

## Run the demonstration

Python 3.14 is tested. The application uses Django 5.2 LTS, SQLite, and local static assets; Docker, Node.js, and external accounts are not required. Dependency installation needs internet access.

```bash
bash scripts/setup.sh --demo
bash scripts/start.sh
```

Open **http://127.0.0.1:8000**. The start script runs the web server and deadline worker together; Ctrl-C stops both.

The explicit `--demo` option creates 15 synthetic instruments across two institutions. It never runs automatically in production, refuses an unmarked nonempty database, and preserves existing records and passwords when repeated.

All demonstration accounts use **`DemoPass!26265`** unless a different password was supplied to `seed_demo --password`.

| Username | Role | Access |
| --- | --- | --- |
| `institution` | Institution operator | Sahyadri Institute demonstration records |
| `institution2` | Institution operator | Eastern Technical College demonstration records |
| `reviewer` | Regulatory reviewer | Both demonstration institutions |
| `approver` | Competent authority | Both demonstration institutions |
| `auditor` | Read-only auditor | Both demonstration institutions |
| `admin` | System administrator | Accounts, scopes, institutions, and policies; no financial approval authority |

**Demo records, PDFs, bank confirmations, policies, timestamps, and execution references are synthetic. No bank, AICTE system, or payment service is contacted.** The dashboard identifies the demonstration workspace.

### Manual setup / existing environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

If the operating system lacks `ensurepip`, the setup script uses this fallback without changing system Python packages:

```bash
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install -r requirements-dev.txt
```

Run monitoring in a separate terminal when starting the web server manually:

```bash
.venv/bin/python manage.py monitor_deadlines --interval 60
```

For one scheduled pass:

```bash
.venv/bin/python manage.py monitor_deadlines --once
```

### Start with an empty database

Omit `--demo`, migrate, and create the first administrator:

```bash
bash scripts/setup.sh
.venv/bin/python manage.py create_portal_admin --username administrator --email administrator@example.org
```

The command prompts for the password. Sign in, open **Administration**, create institutions and an applicable policy, then create accounts with explicit institution assignments. Reviewers, authorities, and auditors with no assigned institutions cannot access financial records. Users can change their passwords through their account link; an operator can reset an existing password with `manage.py changepassword USERNAME`.

## Use the workflows

1. **Submit an FDR:** sign in as an institution operator, choose **New deposit**, enter the bank/reference, amount, dates, and policy, then attach the required documents. Submission stores a permanent revision.
2. **Scrutinize:** sign in as a reviewer, open the verification queue, and start review. Record observations, request corrections, reject, or accept. Acceptance alone does not establish bank confirmation.
3. **Record independent evidence:** upload the independent verification document as a reviewer, then record the authorized source, method, reference, date, and independently confirmed fields. Values that disagree with the submission must be recorded as a discrepancy. Institution-uploaded bank letters alone do not qualify.
4. **Correct a returned case:** update permitted fields, add documents, and resubmit. Prior submissions and documents remain accessible. A material correction invalidates earlier confirmation of the changed values.
5. **Renew or replace:** open an accepted instrument and choose **Renew / replace**. Its successor follows the same submission workflow. Only acceptance supersedes the original; pending or rejected successors leave it unchanged.
6. **Release, discharge, or refund:** an institution submits a request and release evidence. A reviewer assesses every policy condition and recommends approval. A distinct competent authority approves or rejects. After approval, a reviewer records the real external execution reference, date, and completion evidence. The instrument remains held until that final step. Refunds close the full instrument principal; this application does not execute payments or calculate interest/partial balances.
7. **Monitor and report:** use notifications and scoped worklists for approaching dates, pending scrutiny, missing documents, discrepancies, overdue cases, and release requests. CSV import includes validation and confirmation before creating drafts; required documents and review still follow.

Only one renewal or release process may be open for an instrument. Withdraw an abandoned request or resolve it before starting another. Maturity never automatically authorizes release.

The initial illustrative reminder schedule is 90, 60, 30, 7, and 1 day before deadlines, with a 7-day review timeline. Create a new policy version to change requirements; cases retain their recorded policy version. These values do not assert actual AICTE requirements.

## Architecture and data

| Component | Responsibility |
| --- | --- |
| `portal/models.py`, `services.py`, `access.py` | Persistent records, authorized transitions, scoped access, concurrency checks, audit, lifecycle notifications |
| `portal/documents.py` | Validated PDF/JPEG/PNG uploads, AES-GCM encryption, integrity checks, private retrieval |
| `portal/views.py`, `forms.py`, templates/static | Server-rendered responsive UI, form validation, administration, CSV preview/import/export |
| `portal/monitoring.py`, `reporting.py` | Deadline evaluation, scoped reminders, durable SMTP outbox, portfolio metrics |
| `portal/management/commands/` | Worker, demo setup, administrator bootstrap, SQLite backup/recovery |
| `config/settings.py` | Environment-driven development/production configuration |

Local state lives in the ignored `.local/` directory:

- `portal.sqlite3`: users, institutions, instrument metadata, cases, audit, sessions, notifications.
- `documents/`: encrypted immutable uploads, never exposed as public media.
- `secrets.json`: generated development signing/encryption keys, private file permissions.
- `demo-mode`: marker created only by explicit demo seeding.

Database metadata is **not encrypted by SQLite**. Production requires encrypted database/document volumes, restricted operating-system access, and HTTPS. Losing the document encryption key makes the stored evidence unrecoverable. Changing that key without re-encrypting files is unsupported.

Important safeguards:

- Authorization is checked on mutations, downloads, reports, exports, and notifications; changing a URL does not expand institution scope.
- Financial transitions use transactions, record versions, and database constraints. State changes, audit, and lifecycle notifications commit together. Stale decisions are rejected.
- Audit events are append-only through the application. This does not claim protection against a database or infrastructure administrator.
- Documents are capped at 10 MB and parsed to validate their content. Downloads are permission-checked attachments with no-store headers. No antivirus service is bundled.
- Django sessions, password validation/hashing, CSRF controls, login throttling, and production secure-cookie settings protect authentication. All assets are served locally.
- In-app notifications work without external services. SMTP statuses distinguish queued, failed, and **accepted by the mail server**; acceptance does not prove recipient delivery. Retries are durable, but a crash around SMTP acceptance can cause a duplicate email.
- Dashboard categories may overlap: expiring instruments are a subset of active instruments. Accepted matured obligations remain in held principal until release; superseded predecessors are excluded from current portfolio totals. Measured turnaround uses recorded submission/decision timestamps; no improvement percentages are invented.

## Configuration and deployment

Copy `.env.example` to `.env` or supply environment variables. `.env` is ignored by Git; existing process environment values take precedence. `DEBUG` defaults to local development and generates private local keys. Production (`DJANGO_DEBUG=false`) refuses startup without explicit strong secrets and allowed hosts.

| Variable | Purpose |
| --- | --- |
| `DJANGO_DEBUG` | `true` locally; explicitly `false` for production |
| `DJANGO_SECRET_KEY` | Independent random signing secret, at least 40 characters |
| `DOCUMENT_ENCRYPTION_KEY` | URL-safe base64 encoding of exactly 32 random bytes; preserve separately |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames |
| `DATABASE_URL` | Optional PostgreSQL URL, e.g. `postgresql://USER:PASSWORD@HOST:5432/fdr?sslmode=require` |
| `SQLITE_PATH` | Alternative SQLite file when PostgreSQL is not configured |
| `PRIVATE_STORAGE_ROOT` | Private encrypted-document directory |
| `CSRF_TRUSTED_ORIGINS` | HTTPS origins when deployment requires them |
| `TRUST_PROXY_HTTPS` | Enable only behind a trusted proxy that replaces `X-Forwarded-Proto` |
| `SMTP_ENABLED` | `false` by default; enables actual SMTP delivery when configured |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | Authorized SMTP connection settings |

Generate independent keys locally; store them in a secret manager or private environment configuration:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

For a production host, install `requirements.txt`, configure secrets/storage/PostgreSQL, and run:

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2
```

Supervise the web process and `manage.py monitor_deadlines --interval 60` as separate services. Serve `staticfiles/` at `/static/` through a TLS-enabled reverse proxy. **Never serve `.local/` or `PRIVATE_STORAGE_ROOT` as static/media files.** Restrict Gunicorn to loopback or a Unix socket. If the proxy terminates TLS, configure it to overwrite the protocol header, then opt in with `TRUST_PROXY_HTTPS=true`; do not trust client-supplied forwarding headers on a publicly reachable backend. The `/health/` endpoint checks database availability; the dashboard shows the monitoring heartbeat and errors.

PostgreSQL configuration and migrations are provided, but deployment-specific transaction behavior must be verified against the actual PostgreSQL instance. Docker is optional and was not available for runtime verification here. Bank/AICTE integration credentials, contracts, and authorization were not supplied, so there are no fabricated provider endpoints or live connectivity claims.

## Backup and recovery

The local backup command takes a consistent SQLite snapshot and copies only the encrypted documents referenced by that snapshot. Every archived file has a SHA-256 checksum. The ZIP contains confidential database metadata and must be protected; it excludes keys by default.

```bash
.venv/bin/python manage.py backup_portal --output backups/fdr-backup.zip
.venv/bin/python manage.py restore_portal backups/fdr-backup.zip --destination /tmp/fdr-recovery
```

Existing archives are never overwritten. Recovery validates inventory, paths, types, checksums, database integrity, and referenced documents, then writes into a separate new/empty directory. It does not modify the running portal. Preserve the original `DOCUMENT_ENCRYPTION_KEY` separately; without it the encrypted documents cannot be opened.

To use a verified recovery, stop the web/worker processes, configure `SQLITE_PATH=/tmp/fdr-recovery/portal.sqlite3` and `PRIVATE_STORAGE_ROOT=/tmp/fdr-recovery/documents`, provide the original encryption key, and restart. Verify a known document and record before switching users to it. Keep the previous database and document directory intact for rollback.

For a disposable development backup only, `--include-dev-keys` also archives the effective keys. This option is rejected with `DEBUG=false`; protect such an archive as a secret. The command does not perform a PostgreSQL backup: use coordinated `pg_dump`/restore and private-volume snapshots, preserving keys separately, and test restoration on the deployment environment.

## Verification

Run focused application tests and schema checks:

```bash
.venv/bin/python manage.py test portal.tests.test_workflows portal.tests.test_monitoring portal.tests.test_views portal.tests.test_operations
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

Browser journeys use a real locally installed Chrome/Chromium executable and are opt-in:

```bash
RUN_BROWSER_TESTS=1 CHROME_PATH=/opt/google/chrome/chrome .venv/bin/python manage.py test portal.tests.test_browser
```

Set `CHROME_PATH` to the actual executable on your system. The browser tests start their own local server and exercise submission/correction/resubmission and separate release approval/execution; they do not call bank/payment services. Browser absence is an unavailable check, not a successful browser test.

The automated proof covers all six problem-statement scenarios plus institution isolation, document validation/encryption, stale transitions, policy immutability, scope-aware notifications, scheduled catchup/deduplication, SMTP error/retry behavior, CSV validation, authentication controls, and safe backup restoration. Live bank, AICTE, SMTP delivery, infrastructure encryption, and PostgreSQL deployment checks require their corresponding authorized external environments.

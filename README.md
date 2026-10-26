# CampusPass

**A backend event-registration API designed to prevent overbooking when many students compete for the last seats.** CampusPass uses PostgreSQL transactions and row locking for capacity-aware registrations, automatically promotes students from a FIFO waitlist when a confirmed registration is cancelled, and issues versioned HMAC-signed QR tickets for check-in.

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)](https://fastapi.tiangolo.com/) [![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%2B-336791)](https://www.postgresql.org/)

**Project:** TechSpace Tech Team Recruitment — Backend Domain  
**Type:** Backend-only REST API (no separate frontend)

## Live Demo

- **API base URL:** https://campuspass-cizj.onrender.com
- **Interactive API documentation:** https://campuspass-cizj.onrender.com/docs
- **Health check:** https://campuspass-cizj.onrender.com/health
- **Database readiness:** https://campuspass-cizj.onrender.com/health/ready
- **Source code:** https://github.com/visionnn777/campuspass

The deployment health checks were verified: `/health` returned `{"status":"ok"}` and `/health/ready` returned a successful status with the database connected and migration `0002` applied. These are deployment smoke checks; they are not a claim that the full automated test suite has passed on the deployed service.

The root path `/` is not a landing page; use `/docs` to explore the API. The hosted API may take longer to respond after a period of inactivity because the hosting service can spin down free instances. Hosting plan and database retention policies may change; check the provider's current terms if long-term availability is required.

## The Problem

College events are often managed with forms and spreadsheets. This can lead to overbooked events, manually managed waitlists, and weak ticket validation. CampusPass provides backend workflows for event management, registration, waitlisting, and check-in.

| Challenge | CampusPass approach |
|---|---|
| Multiple students compete for the final seat | Lock the event row in PostgreSQL before counting available capacity and confirming a registration |
| Waitlists are handled manually | FIFO waitlist with promotion inside the cancellation transaction |
| Tickets can be copied or reused | HMAC-signed, versioned tickets with single-use check-in |
| Privileged actions need access control | Student, organiser, and admin roles with ownership and permission checks |

## Features

- **Authentication and accounts:** registration, JSON login, JWT bearer authentication, current-user endpoint, and student/organiser/admin roles.
- **Admin controls:** user listing and management, activation controls, and safeguards against self-demotion or removing the last active administrator.
- **Event management:** draft creation, editing, publishing, cancellation, deletion of drafts, public listing, search, category/date filters, sorting, and pagination.
- **Registrations:** repeat-safe registration requests, capacity enforcement, registration cancellation, attendee lists, and organiser removal.
- **Waitlist:** FIFO queue positions and automatic promotion after a confirmed student cancels.
- **Tickets and check-in:** JSON or QR PNG ticket, ticket reissue, invalidation of older ticket versions, check-in time window, and single-use scans.
- **Operational safeguards:** request IDs, JSON logs, request-body limit, keyed rate limiting, database lock/statement timeouts, and structured error responses.
- **Schema management:** versioned database migrations using Alembic.

## Technology Stack

| Purpose | Technology |
|---|---|
| Language and API framework | Python 3.12, FastAPI, Uvicorn |
| Database | PostgreSQL 16 or later |
| ORM and schema migrations | SQLAlchemy 2.x, Alembic |
| Database driver | Psycopg 3 |
| Validation and configuration | Pydantic v2, pydantic-settings |
| Password hashing | Argon2id through `pwdlib` |
| Authentication tokens | PyJWT, signed JWT bearer tokens |
| Rate limiting | `limits` (in-memory) |
| QR generation | `qrcode` / Pillow |
| Testing | pytest, HTTPX |

## Try the Live API

1. Open the [interactive API documentation](https://campuspass-cizj.onrender.com/docs).
2. Review the endpoints and their request/response schemas.
3. Register a normal user through `POST /api/v1/auth/register` and then log in through `POST /api/v1/auth/login`.
4. Copy the returned `access_token`, click **Authorize** in Swagger UI, and enter the token as requested by the interface.
5. Use the authenticated endpoints available to that user's role.

New registrations are students by default. Organiser and admin operations require the appropriate role; do not expect a newly registered account to have elevated permissions. Admin credentials are not published in this repository. If a judge requires a privileged demo account, arrange access separately and share credentials privately rather than committing them to GitHub.

## Run Locally on Windows (PowerShell)

### Prerequisites

- Python 3.12
- PostgreSQL 16, 17, or 18
- Git (optional if downloading the repository ZIP)

### 1. Get the project and create a virtual environment

Clone the repository, or download the ZIP from GitHub and extract it. Open the project root in VS Code—the folder containing `app/`, `alembic.ini`, and `requirements-dev.txt`.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks virtual-environment activation, run this once in your user account and then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Create a local role and databases

Start PostgreSQL, then open `psql` as the PostgreSQL administrator. Adjust the installation path/version if needed:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres
```

In the `psql` prompt, create a project role and two separate databases. Replace the example password with a strong, URL-safe password. If the role or databases already exist, inspect them first instead of creating them again.

```sql
CREATE ROLE campuspass LOGIN PASSWORD 'replace_with_a_strong_url_safe_password';
CREATE DATABASE campuspass OWNER campuspass;
CREATE DATABASE campuspass_test OWNER campuspass;
\q
```

### 3. Configure environment variables

Copy the example file and open it for editing:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `DATABASE_URL` and `TEST_DATABASE_URL` to the corresponding local databases. Use the same database role/password, but keep the database names different:

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+psycopg://campuspass:YOUR_PASSWORD@localhost:5432/campuspass
TEST_DATABASE_URL=postgresql+psycopg://campuspass:YOUR_PASSWORD@localhost:5432/campuspass_test
```

Generate two separate secrets by running this command twice:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the first output in `JWT_SECRET` and the second in `TICKET_SECRET`. Each must be at least 64 characters long and they must be different. Keep `.env` private; never commit it or paste secrets into issue reports or screenshots.

### 4. Install dependencies and create the schema

The repository includes compiled dependency files: `requirements.txt` for the runtime and `requirements-dev.txt` for the runtime plus test dependencies. The `.in` files are the direct-dependency input files used to generate those compiled files.

For running the API locally:

```powershell
pip install -r requirements.txt
alembic upgrade head
```

For running the test suite, install the development dependencies instead (this includes the runtime dependencies):

```powershell
pip install -r requirements-dev.txt
alembic upgrade head
```

Do not edit generated lock files manually. If dependencies change, update the corresponding `.in` file and recompile the lock file using `uv pip compile` with Python 3.12.

### 5. Create an administrator and local demo data

```powershell
python -m scripts.create_admin
python -m scripts.seed_demo
```

The admin script prompts for an email, name, and password. The demo seeding script is intended for development only and refuses production mode. Optional development-only commands:

```powershell
python -m scripts.seed_demo --reset-demo-accounts
python -m scripts.seed_demo --new-event
```

`--reset-demo-accounts` resets credentials/roles only for the script's known demo accounts. Do not use demo seeding to populate the production database.

### 6. Start the API

```powershell
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs**. Log in using `POST /api/v1/auth/login`, copy the returned `access_token`, select **Authorize**, and use the authenticated routes allowed by the account's role.

For macOS/Linux, activate the virtual environment with `source .venv/bin/activate`; the other project commands are equivalent.

## Run Tests Safely

Tests require PostgreSQL; SQLite is not a substitute for the row-locking and concurrency behaviour this project tests. The test configuration verifies that the active database is exactly `campuspass_test`. The test suite clears data from the test database between tests, so **never point `TEST_DATABASE_URL` at a production or personal database**.

With `.env` correctly configured and the test database created, run:

```powershell
pytest -v
pytest -m concurrency -v
```

The suite includes tests for authentication, permissions, events, registrations, tickets, database constraints, request middleware, rate limits, and concurrency scenarios. Record the actual test output before claiming that all tests pass; deployment health checks alone do not prove the full suite passes.

## API Overview

All business endpoints use the `/api/v1` prefix. The complete schemas, parameters, and responses are available in [Swagger UI](https://campuspass-cizj.onrender.com/docs).

| Method | Endpoint | Access |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Public |
| `POST` | `/api/v1/auth/login` | Public |
| `GET` | `/api/v1/auth/me` | Authenticated user |
| `GET` | `/api/v1/events` and `/api/v1/events/{id}` | Public, subject to draft visibility rules |
| `POST`, `PATCH`, `DELETE` | Event management endpoints | Event owner organiser or admin, as applicable |
| `PUT`, `GET`, `DELETE` | `/api/v1/events/{id}/registrations/me` | Authenticated user |
| `GET`, `DELETE` | Event attendee/registration management endpoints | Event owner organiser or admin, as applicable |
| `GET`, `POST` | Ticket, ticket reissue, and check-in endpoints | Authenticated, with organiser/admin checks for check-in |
| `GET` | `/api/v1/me/registrations` and `/api/v1/me/events` | Authenticated user / organiser, as applicable |
| `GET`, `PATCH` | `/api/v1/admin/users` and `/api/v1/admin/users/{id}` | Admin |
| `GET` | `/health` and `/health/ready` | Public |

Errors use a consistent structure containing an error code, message, and request ID. Refer to the live documentation for the exact response contract.

## Important Design Decisions

- **Lock the event before counting seats:** serializes capacity decisions so two requests cannot both claim the final seat.
- **Promote the FIFO waitlist in the cancellation transaction:** keeps the cancellation and promotion operation consistent.
- **Use versioned, signed tickets:** reissue or cancellation invalidates older ticket versions.
- **Load roles from the database:** role changes and deactivation are reflected without waiting for an old JWT to expire.
- **Use Alembic for schema changes:** database schema creation is migration-driven rather than based on automatic `create_all()` calls.
- **Use one application worker with in-memory rate limits:** the current rate limiter is not shared between multiple processes and resets on restart.

## Current Limitations

- Rate limits are in-memory and are not shared across multiple application processes.
- JWT revocation/denylisting and refresh tokens are not implemented.
- Email verification and registration notifications are not implemented.
- Promoted students see their updated registration status when they next query the API; there is no notification service.
- The full PostgreSQL integration test suite has not been confirmed as passing for the deployed revision. Run the tests and report the actual result before treating concurrency guarantees as verified.
- The service is deployed on Render; free-tier availability and database retention are subject to the provider's current policies.

## My Contribution

My contribution to CampusPass focuses on the backend implementation: REST API design, authentication and role-based access control, PostgreSQL data modelling and Alembic migrations, event registration and capacity management, FIFO waitlist promotion, signed ticket/check-in workflows, input validation, error handling, and deployment documentation. I can walk through the relevant code, explain the design choices, and demonstrate the API during the TechSpace review.

## Project Structure

```text
app/
  core/       configuration, database, locks, security, tickets, rate limiting, middleware
  models/     user, event, registration
  schemas/    Pydantic request and response schemas
  services/   business logic for auth, admin, events, registrations and tickets
  routers/    HTTP endpoints
alembic/      database migrations
scripts/      create_admin, seed_demo, export_openapi
tests/        application and PostgreSQL integration tests
docs/         OpenAPI contract, design decisions, learning guide and notices
licenses/     third-party licence information
```

## Documentation and Notices

- [Live Swagger documentation](https://campuspass-cizj.onrender.com/docs)
- [OpenAPI contract](docs/openapi.json)
- [Project decisions](docs/DECISIONS.md)
- [Learning guide](docs/LEARNING_GUIDE.md)
- [Third-party notices](docs/THIRD_PARTY_NOTICES.md)

## Future Improvements

Potential next steps include Docker Compose for one-command local setup, automated CI with a PostgreSQL service, Redis-backed rate limits, explicit idempotency-key response replay, refresh tokens and revocation, verified college email, notifications, and a measured load-test report.

# CampusPass API: Engineering Specification v3.0

A backend-only REST API for college events. Students register for events. Seats can never be overbooked, a fair waitlist fills freed seats automatically, and each ticket can be used for check-in only once, even when many requests arrive at the same moment.

- **Purpose:** TechSpace Tech Team recruitment, Backend domain
- **Development machine:** Windows · Python 3.12 · local PostgreSQL (16 or newer recommended, see §1)
- **Status of this document:** design only. Nothing here is claimed as implemented or tested until §25's checklist is ticked with real test output.

v3.0 merges spec v2.1 with the review document "CampusPass v2.2 Required Changes". Every item in that review is checked and resolved in **Appendix A**. Issues found during this second review are in **Appendix B**.

### Contents
0. Principles and phases · 1. Tech stack · 2. Architecture · 3. Data model · 4. Registration state machine · 5. Authentication and authorization · 6. Rate limiting and abuse controls · 7. Events · **8. The lock protocol (core)** · 9. Registration · 10. Cancellation and waitlist promotion · 11. Tickets and check-in · 12. Errors, DB exceptions and observability · 13. Security checklist · 14. Testing · 15. Configuration and repo hygiene · 16. Windows setup · 17. Dependencies and locking · 18. File tree · 19. API reference · 20. Reference SQL · 21. Implementation plan · 22. README outline · 23. Demo script · 24. Cross-question prep · 25. Definition of done · Appendix A: v2.2 review tally · Appendix B: new findings · Appendix C: version history

---

## 0. Principles and phases

### Principles
1. **The database enforces correctness.** Row locks and constraints decide what happens. Python checks only produce friendlier error messages.
2. **One lock protocol, applied everywhere** (§8). Every operation that depends on an event's state follows the same lock order.
3. **Explainability beats feature count.** No layer, tool or library is added just to look sophisticated (no repository layer, no message queue).
4. **Honest claims.** The README lists only features that are implemented *and* tested, with the actual test command and its observed result. Everything else goes under Limitations or Roadmap.
5. **A smaller, verified system beats a larger, unverified one.**

### Phases

| Phase | Scope | Claim in the README? |
|---|---|---|
| **Phase 1: Core** (the submission) | Auth (JSON login, JWT, roles, admin script) · Event CRUD, publish, cancel, delete drafts · Listing with search, filters, sort and pagination · Lock protocol · Registration (PUT) · Cancellation and organiser removal · FIFO waitlist with promotion · Versioned HMAC tickets, QR codes, check-in · Rate limiting with explicit keys · Request size limit · Precise DB-error mapping and lock timeouts · Alembic migrations · Locked dependencies · The full test catalogue in §14 · README | Yes, once tested |
| **Phase 2: Hardening** | Audit log table · GitHub Actions CI with a Postgres service · Docker Compose · Reproducible load-test report · Deployment behind a trusted proxy | Only after each item works |
| **Phase 3: Roadmap** | Redis-backed shared rate limits · `Idempotency-Key` response replay · Refresh tokens and revocation (a `jti` denylist) · Email verification and notifications · Trigram search · Metrics | No, roadmap only |

---

## 1. Tech stack

| Purpose | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Stable, with full wheel support for every dependency. 3.14 is also installed, but the newest Python release can lag behind on library support. Selected with `py -3.12`. |
| Framework | FastAPI | Automatic OpenAPI/Swagger docs, dependency injection, Pydantic validation |
| Database | **PostgreSQL 16, 17 or 18** | Row locks (`FOR UPDATE` / `FOR SHARE`), CHECK and UNIQUE constraints, sequences. See the version policy below. |
| Driver | psycopg 3 (`psycopg[binary]`) | The current driver, with pre-built Windows wheels. It exposes `sqlstate` and `diag.constraint_name` for precise error mapping. |
| ORM | SQLAlchemy 2.x, synchronous | One `Session` per request. Sync routes run in FastAPI's thread pool, which is simple to reason about. |
| Migrations | **Alembic** (from Phase 1) | Versioned, reviewable schema changes. `create_all()` is not used. |
| Config | pydantic-settings | Typed settings from `.env`. The app refuses to start if anything is invalid. |
| Validation | Pydantic v2 | `extra="forbid"` blocks mass assignment, and `AwareDatetime` rejects timestamps without a timezone |
| Passwords | `pwdlib[argon2]` (Argon2id) | OWASP's first choice, and what FastAPI's tutorial uses. No 72-byte bcrypt limit. |
| Tokens | PyJWT, HS256 **pinned** | Required claims plus issuer and audience checks (§5) |
| Tickets | Python stdlib `hmac` + `hashlib` (HMAC-SHA256) | Versioned, signed tickets (§11). No new dependency. |
| Rate limiting | `limits` (MemoryStorage, moving window) | Called from a FastAPI dependency with explicit keys (per IP, per account, per email). Switching to Redis later is a one-line storage change. |
| QR | `qrcode[pil]` | Returns a PNG |
| Tests | pytest + httpx + a live uvicorn test server | Real HTTP, real PostgreSQL, a thread barrier for true concurrency |
| Lock file | `uv pip compile --universal` | One cross-platform pinned file that works on Windows and in Linux CI |

### PostgreSQL version policy
Checked on 26 Oct 2026:

| Major version | Final release (end of life) | Use it? |
|---|---|---|
| 13 and older | 13 Nov 2025 | ❌ unsupported |
| 14 | **12 Nov 2026**, about two weeks away | ❌ don't start a new project on it |
| 15 | 11 Nov 2027 | ⚠️ acceptable, but upgrade soon |
| **16 / 17 / 18** | 9 Nov 2028 / 8 Nov 2029 / 14 Nov 2030 | ✅ recommended |

Run `psql --version`. If it shows 15 or older, install 18 (or 17) for this project. Always use the latest minor release of your major version, and re-check the official policy before deploying.

---

## 2. Architecture

```
routers/    HTTP only: parse input, call one service function, shape the response
schemas/    Pydantic input (extra="forbid") and explicit output models
services/   business rules + transaction boundaries + locks (§8 lives here)
models/     SQLAlchemy tables, constraints (named by convention)
core/       config, security, tickets, clock, rate limits, errors, DB session, middleware
alembic/    migrations
```

- **One `Session` per request** through the `get_db()` dependency. It's never shared across threads. The session is **rolled back** on any exception before the error response is built.
- **Transactions are owned by services.** A service function = one transaction = one business operation.
- **No repository layer.** Services use SQLAlchemy directly, so there is one less abstraction to explain.
- **Constraint naming convention** on SQLAlchemy `MetaData`: `uq_%(table_name)s_%(column_0_name)s`, `ck_%(table_name)s_%(constraint_name)s`, `fk_...`, `ix_...`. Deterministic names let Alembic diffs stay stable and let §12 map violations by name.
- **Clock abstraction:** `app/core/clock.py` has `now_utc()`. Every time-based rule reads it **after taking locks** (§8). Tests swap it for a fixed clock to test registration windows precisely.

---

## 3. Data model

All timestamps are `timestamptz` and stored as UTC. API inputs must carry an offset (for example `2026-11-05T10:00:00+05:30`). Inputs without one are rejected with 422. Responses are in UTC (ISO-8601 with `Z`), and clients convert to local time.

### users
| Column | Rule |
|---|---|
| id | UUID PK |
| email | normalized (trimmed and lower-cased) in the app **and** enforced by `CHECK (email = lower(btrim(email)))` · `UNIQUE` (named `uq_users_email`) |
| password_hash | Argon2id (PHC string format). Never serialized. |
| full_name | 1–100 characters, trimmed |
| role | `student` · `organiser` · `admin` (default `student`) |
| is_active | bool, default true. Users are **deactivated, never deleted**. |
| created_at, updated_at | timestamptz |

*Note:* lower-casing the part of the email before the `@` is technically stricter than RFC 5321 allows, but every mainstream provider treats it case-insensitively. This is a documented, deliberate choice.

### events
| Column | Rule |
|---|---|
| id | UUID PK |
| organiser_id | FK → users (RESTRICT) |
| title | 3–150 · description ≤ 5000 · venue 1–200 |
| category | enum `tech`, `cultural`, `sports`, `workshop`, `other` |
| registration_opens_at, registration_closes_at, starts_at, ends_at | timestamptz, with the ordering CHECK below |
| capacity | 1–10000 |
| waitlist_limit | 0–10000, default 50. **0 = no waitlist.** |
| status | `draft` · `published` · `cancelled` |
| cancelled_at | set when cancelled |
| created_at, updated_at | timestamptz |

CHECK: `registration_opens_at < registration_closes_at <= starts_at < ends_at`.

**Derived, never stored:** `is_completed` (now ≥ `ends_at`), `is_registration_open`, `seats_left`, `waitlist_count`. These are **snapshots** for display. The authoritative capacity decision is always made under the lock (§9).

### registrations (one row per user per event, reused across cycles)
| Column | Rule |
|---|---|
| id | UUID PK |
| event_id | FK → events (RESTRICT) |
| user_id | FK → users (RESTRICT) |
| status | `confirmed` · `waitlisted` · `cancelled` · `checked_in` |
| queue_no | BIGINT from sequence `waitlist_queue_seq`. Set **only** while waitlisted. |
| ticket_version | INT, default 1, ≥ 1. Increased on reissue and on cancellation (§11). |
| confirmed_at, waitlisted_at, cancelled_at, checked_in_at | timestamptz, following the cycle rules below |
| cancelled_by | FK → users, nullable (the student themselves, or the organiser or admin who removed them) |
| checked_in_by | FK → users, nullable |
| created_at, updated_at | timestamptz |

**Constraints** (named, and mapped in §12):
- `uq_registrations_event_id` on `(event_id, user_id)`: at most one row per student per event.
- `ck_registrations_waitlist_fields`: `(status='waitlisted' AND queue_no IS NOT NULL AND waitlisted_at IS NOT NULL) OR (status<>'waitlisted' AND queue_no IS NULL)`.
- `ck_registrations_confirmed_fields`: `status NOT IN ('confirmed','checked_in') OR confirmed_at IS NOT NULL`.
- `ck_registrations_checkin_fields`: `(status='checked_in' AND checked_in_at IS NOT NULL AND checked_in_by IS NOT NULL) OR (status<>'checked_in' AND checked_in_at IS NULL AND checked_in_by IS NULL)`.
- `ck_registrations_cancel_fields`: `(status='cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) OR (status<>'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)`. Every cancellation records an actor.
- `ck_registrations_ticket_version`: `ticket_version >= 1`.

These constraints protect the **consistency of each row**. They do **not** enforce total event occupancy. Occupancy is protected by the lock protocol (§8). **Hook: constraints guard each seat card; the lock guards the hall door.**

### Cycle and timestamp rules
A **cycle** starts when a student registers, or re-registers after cancelling. The timestamps describe the **current cycle only**. The full history of transitions belongs in the Phase 2 audit log.

| Transition | Field changes |
|---|---|
| none → confirmed | `confirmed_at = t` |
| none → waitlisted | `waitlisted_at = t`, `queue_no = nextval` |
| waitlisted → confirmed (promotion) | `confirmed_at = t`, `queue_no = NULL` (`waitlisted_at` is kept, which shows the person was promoted) |
| waitlisted → cancelled | `queue_no = NULL`, `cancelled_at = t`, `cancelled_by = actor` |
| confirmed → cancelled | `cancelled_at = t`, `cancelled_by = actor`, **`ticket_version += 1`** |
| confirmed → checked_in | `checked_in_at = t`, `checked_in_by = scanner` |
| cancelled → confirmed or waitlisted (re-registration) | **clear** `confirmed_at, waitlisted_at, cancelled_at, cancelled_by`, then apply the "none →" row. `ticket_version` is unchanged, because the cancel already increased it. |

**Sequence gaps are normal.** `nextval` is never rolled back, so aborted transactions leave gaps (for example 101, 102, 105). FIFO order is still exact, because numbers are taken while holding the event lock. Students never see `queue_no`. They see `queue_position` = 1 + the number of waitlisted rows for this event with a smaller `queue_no`.

---

## 4. Registration state machine

```
              seat free                     cancel (before start)
  (none) ───────────────► confirmed ───────────────────────────► cancelled
     │                      │   ▲                                    │
     │ full + waitlist      │   │ promoted (seat freed or capacity   │ re-register (PUT)
     │ has room             │   │ raised, before start, user active) │ → seat, or back
     ▼                      │   │                                    │   of the queue
  waitlisted ───────────────────┘                                    │
     │ cancel               │ scanned at the door                  ◄─┘
     ▼                      ▼
  cancelled             checked_in  (final)
```
- Exactly one row and one status per student per event.
- `checked_in` is final. Cancelling after check-in returns `409 CANNOT_CANCEL_AFTER_CHECKIN`.
- Re-registration never jumps the queue, because a new `queue_no` is always the largest.

---

## 5. Authentication and authorization

### Signup: `POST /api/v1/auth/register`
- Body `{email, password, full_name}` with `extra="forbid"`. Sending `role`, `is_active` or `id` returns **422**. New accounts are always `student`.
- Optional `ALLOWED_EMAIL_DOMAINS` (for example your college's domain) restricts who can sign up. Without email verification (Phase 3) this is only a soft control, and the README says so.
- A duplicate email returns `409 EMAIL_TAKEN`. That confirms the email exists (an accepted trade-off), which is limited by the signup rate limit.

### Password policy (NIST SP 800-63B-4, 2025)
- **Minimum 15 characters.** NIST's current rule for passwords used as the only factor, which is the case here.
- **Maximum 128.** NIST asks that at least 64 be allowed. The cap prevents hashing-cost abuse.
- **No composition rules** (no forced symbols or digits). NIST says these must not be imposed, because they lead to predictable patterns.
- **Blocklist check:** reject passwords found in a bundled common-password list (`app/data/common_passwords.txt`, for example the 10k most common, committed once), plus context words: the email's local part, "campuspass", the user's name.
- Passwords are Unicode-normalized (NFC) before hashing.
- **Hook: long and unusual beats short and complicated.** "Nehru Place chai at 4 pm" beats "P@ssw0rd1".

### Login: `POST /api/v1/auth/login`
- **JSON body** `{email, password}` returns `{access_token, token_type: "bearer", expires_in}`.
- **Not** the OAuth2 password grant. RFC 9700 (OAuth 2.0 Security BCP, §2.4) says the Resource Owner Password Credentials grant *MUST NOT be used*. CampusPass is a first-party API issuing its own tokens, **not an OAuth authorization server**, and it never claims to be one.
- OpenAPI declares an **HTTP Bearer** scheme. In Swagger: call `/auth/login`, copy the token, click **Authorize**, paste it.
- Unknown email and wrong password both return the same `401 INVALID_CREDENTIALS`. For an unknown email, the server verifies against a **dummy Argon2 hash**, so the response time doesn't reveal which emails exist.
- A correct password on a disabled account returns `403 ACCOUNT_DISABLED`.

### JWT
- PyJWT, `algorithms=["HS256"]` **hard-coded**. The algorithm is never taken from the token header (RFC 8725 §3.1).
- Claims: `sub` (user UUID), `iat`, `exp` (now + 30 min), `iss` = `JWT_ISSUER` (default `campuspass`), `aud` = `JWT_AUDIENCE` (default `campuspass-api`), `jti` (a random UUID that lets a Phase 3 denylist revoke single tokens).
- Decode with `audience=…`, `issuer=…`, `leeway=10` seconds, `options={"require": ["exp","iat","sub","iss","aud","jti"]}`. PyJWT only checks `exp`/`iat` *if present*, so `require` forces them to be present. `sub` must parse as a UUID.
- **No role in the token.** Each request loads the user from the DB, so role changes and deactivation take effect **immediately**.
- `JWT_SECRET` must be at least 64 characters (generate with `secrets.token_urlsafe(48)`) and must differ from `TICKET_SECRET`. Otherwise the app refuses to start.
- Missing, invalid or expired tokens return `401` with a `WWW-Authenticate: Bearer` header.
- **Documented limitation:** no refresh tokens and no server-side logout. A token is valid for up to 30 minutes after logout or theft. Revocation is in Phase 3.

### Roles and ownership
| Action | student | organiser | admin |
|---|---|---|---|
| Browse published or cancelled events | ✅ | ✅ | ✅ |
| Register, cancel, get a ticket | ✅ | ✅ (not for their own events) | ✅ |
| Create events | ❌ 403 | ✅ | ✅ |
| Edit, publish, cancel, delete an event | ❌ | own only | any |
| View attendees, remove a registration, check in | ❌ | own events | any |
| Manage users | ❌ | ❌ | ✅ |

- User and owner IDs always come from the verified token, never from the request body.
- Someone else's **draft** returns `404`, not 403, so its existence isn't revealed.

### Admin
- `python -m scripts.create_admin` reads the password interactively with `getpass` and applies the same password policy. This is also the **break-glass** recovery path if every admin is lost.
- `GET /api/v1/admin/users?role=&is_active=&page=`
- `PATCH /api/v1/admin/users/{id}` with `{role?, is_active?}` (at least one field). **This is how organisers are made.**
- **Self-guard:** admins can't demote or disable themselves (`409 CANNOT_MODIFY_SELF`).
- **Admin-change lock order:** the transaction locks active admins, the target user, and the acting admin in ascending UUID order, then refreshes the rows. The acting user must still be active and an admin after the lock; this prevents a request that authenticated before a concurrent demotion/deactivation from using stale authority.
- **Last-admin guard:** if the change would remove an active admin, the transaction refuses if only one active admin remains (`409 LAST_ADMIN`). The combined sorted lock set prevents two admins disabling each other at the same moment and avoids opposite user-row lock ordering. It's the same "lock, then count" pattern as seat booking.

---

## 6. Rate limiting and abuse controls

Implemented as a FastAPI dependency, `rate_limit(scope, limit, key=...)`, using the `limits` library (MemoryStorage, moving-window strategy). Each limit names its key **explicitly**.

| Endpoint | Key | Limit |
|---|---|---|
| `POST /auth/register` | client IP | 5 / hour |
| `POST /auth/login` | client IP | 10 / minute |
| `POST /auth/login` | normalized email (hashed in memory) | 10 / 15 minutes |
| `PUT /events/{id}/registrations/me` | account | 10 / minute |
| `PUT /events/{id}/registrations/me` | client IP | 60 / minute |
| `DELETE /events/{id}/registrations/me` | account | 10 / minute |
| `GET …/ticket` | account | 30 / minute |
| `POST …/ticket/reissue` | account | 5 / hour |
| `POST /events/{id}/checkins` | account (the scanner) | 120 / minute |
| `GET /events`, `GET /events/{id}` | client IP | 120 / minute |

- Exceeding a limit returns `429 RATE_LIMITED` with a `Retry-After` header (seconds until the window frees up).
- **Account-keyed limits run after authentication**, because the dependency depends on `get_current_user`. IP-keyed limits run before.
- **Client IP** = the TCP peer address. `X-Forwarded-For` is honoured **only** when uvicorn runs with `--proxy-headers --forwarded-allow-ips=<exact proxy IP>` (Phase 2 deployment). Otherwise anyone could fake a new IP per request.
- **Per-email login limit trade-off:** an attacker can slow down a victim's login by spamming wrong passwords for their email. The limit is a soft throttle (10 per 15 minutes), not a lockout. This is documented.
- **Phase 1 limitation:** the in-memory counters reset on restart and aren't shared between processes, so the app runs with **one worker**. This is a development and demo limitation.
- **When the limiter is unavailable** (Phase 3, Redis): login and signup **fail closed** (`503 RATE_LIMITER_UNAVAILABLE`), because brute-force protection matters more there. Other endpoints **fail open** and log an error, because availability matters more there.
- `RATE_LIMITS_ENABLED=false` is allowed only when `APP_ENV=test`. Concurrency tests turn limits off, and the dedicated rate-limit tests turn them on.

### Seat hoarding (OWASP API6, sensitive business flows)
Locks stop overbooking. They don't stop one person creating 20 accounts to grab 20 seats. Phase 1 makes this harder with signup and registration limits, the optional email-domain restriction, and organisers being able to **remove** registrations (§10). The real fix, verified college email, is in Phase 3. The README states this honestly.

### Request size limit
A middleware rejects bodies over `MAX_BODY_BYTES` (64 KiB) with `413 PAYLOAD_TOO_LARGE`. Requests with a body but no `Content-Length` get `411 LENGTH_REQUIRED`. Pydantic's field length checks only run **after** the whole body has been read into memory, so they can't stop a 500 MB request. This middleware can.

---

## 7. Events

| Method & path | Who | Rules |
|---|---|---|
| `GET /events` | anyone | Published and cancelled events only. Query: `search` (title, `ILIKE` with `%`/`_` escaped), `category`, `starts_after`, `starts_before`, `include_past` (default **false**: hides ended events), `sort` ∈ {`starts_at`, `-starts_at`, `created_at`, `-created_at`}, `page` ≥ 1, `page_size` 1–100 (default 20). Counts are computed in **one aggregate query**, not one query per event (the N+1 problem). |
| `GET /events/{id}` | anyone (drafts: owner or admin, else 404) | |
| `POST /events` | organiser, admin | Created as `draft` |
| `PATCH /events/{id}` | owner, admin | Rules below |
| `POST /events/{id}/publish` | owner, admin | draft → published. Requires `now < registration_closes_at`. |
| `POST /events/{id}/cancel` | owner, admin | published → cancelled. Not after `ends_at` (`409 EVENT_ENDED`). Drafts are deleted instead (`409 INVALID_STATE`). |
| `DELETE /events/{id}` | owner, admin | Drafts only. Returns 204. Drafts can't have registrations, and FK RESTRICT would block deletion anyway. |
| `GET /me/events` | organiser, admin | The caller's own events in all statuses |

**PATCH rules** (all under the event's `FOR UPDATE` lock, §8):
- Cancelled, started or ended events are read-only (`409`).
- Time ordering is re-validated. A published event's `starts_at` must stay in the future.
- New `capacity` below occupancy (confirmed + checked_in) → `409 CAPACITY_BELOW_OCCUPANCY`.
- **Capacity increase** on a published, not-yet-started event promotes up to `new_capacity − occupancy` people from the waitlist, in FIFO order, in the **same transaction**.
- Lowering `waitlist_limit` below the current waitlist count is allowed. It only blocks new joins, and nobody already waiting is removed.
- `status` and `organiser_id` can't be patched (`extra="forbid"`).

**When an event is cancelled:** registrations are kept unchanged, for history. Registration, promotion, ticket reissue and check-in all stop, because each one re-checks the status under its lock. Students see `event_status: cancelled`.

---

## 8. The lock protocol (the core of the project)

### Rule 1: event first, always
Any transaction that makes a decision based on an event's status, capacity, registration window or check-in window **locks the event row first**. Registration rows come **second**. If the operation also needs to serialize a user's eligibility or role, user rows come **third**, acquired in ascending UUID order. Admin role/status changes lock their actor, target, and currently active admin rows in the same UUID order but do not lock event or registration rows. This common order avoids circular waits between event operations and user administration.

### Rule 2: pick the right strength, and never upgrade
| Lock | Behaviour | Used by |
|---|---|---|
| `SELECT … FOR UPDATE` (exclusive) | One holder at a time. Blocks `FOR SHARE` and all writers. | Anything that **changes the event or its occupancy**: register, cancel, organiser removal, promotion, PATCH, publish, cancel event, delete draft |
| `SELECT … FOR SHARE` (shared) | Many holders at once. Blocks `FOR UPDATE` and all writers. | Anything that **needs the event to stay as it is while it works on one registration**: check-in, ticket reissue |

- Many door volunteers can check people in **in parallel** (shared locks don't conflict with each other), but an event cancellation waits until they finish. Any check-in that arrives after the cancellation commits sees `cancelled` and is rejected.
- **Never upgrade from SHARE to UPDATE inside a transaction.** If two holders of a shared lock both try to upgrade, they deadlock. A transaction that *might* modify the event takes `FOR UPDATE` from the start.
- **Hook: the notice board.** Many people can read it at once (SHARE), but to rewrite it you need everyone to step away (UPDATE).

### Rule 3: decide after locking
Locks are taken first, and **then** the clock (`now_utc()`) is read and the counts are run. In READ COMMITTED mode each statement sees data committed before *it* started, so a count run after waiting for the lock includes the previous holder's booking. The same goes for time: `now()` in SQL returns the **transaction start time**. A request that began at 09:59:59 and waited for the lock until 10:00:01 would otherwise be accepted after a 10:00 deadline. **Hook: lock the door, then look at the clock and count the chairs.**

### Rule 4: bounded waiting
Every DB connection sets `lock_timeout = 3s` and `statement_timeout = 10s`. A stuck transaction can't make every request hang forever. A request that times out gets `503 BUSY` with `Retry-After: 1` (§12).

### Rule 5: short transactions
No network calls, QR rendering, password hashing or emails happen while a lock is held. The QR PNG is rendered **after** the commit.

### Lock table (every state-touching operation)
| Operation | Event row | Registration row(s) | User row(s) | Notes |
|---|---|---|---|---|
| Register (PUT) | FOR UPDATE | own row FOR UPDATE (if it exists) | No additional lock in Phase 1; request authentication checks the caller | §9 |
| Cancel own / organiser removes | FOR UPDATE | target FOR UPDATE, then promotees FOR UPDATE | Promotion locks associated users FOR SHARE in UUID order | §10 |
| PATCH event (incl. capacity) | FOR UPDATE | promotees FOR UPDATE | Promotion locks associated users FOR SHARE in UUID order | §7 |
| Publish / cancel / delete event | FOR UPDATE | – | – | §7 |
| Ticket reissue | FOR SHARE | own row FOR UPDATE | Caller FOR SHARE | Re-check account activity after the lock |
| Check-in | FOR SHARE | target FOR UPDATE | Attendee and scanner FOR SHARE, in UUID order | Re-check both active status and permission; read clock after all locks |
| View ticket (GET) | none | none | none | Read-only. Safety comes from check-in's re-validation (§11). |
| Lists and reads | none | none | none | Snapshot reads |
| Admin role or status change | – | – | Actor, target and active admins FOR UPDATE, in UUID order | Re-check actor after locking; no event/registration locks are acquired in this operation. |

### Why not the alternatives?
- **Python-level checks** have exactly the race we're preventing.
- **SERIALIZABLE isolation** is correct too, but conflicting transactions *fail* with serialization errors and need retry loops. Locks make them *wait*, which is simpler and predictable.
- **An atomic counter** (`UPDATE … SET taken = taken + 1 WHERE taken < capacity`) is faster for very hot events, but the waitlist, re-registration and promotion logic would then be spread across several statements. The event lock keeps everything in one easy-to-follow critical section. At college scale, a lock is held for milliseconds.

---

## 9. Registration: `PUT /api/v1/events/{event_id}/registrations/me`

**Why PUT?** The request means "make sure I'm registered for this event". Repeating it is safe, and it never produces a second seat. PUT has exactly these semantics in HTTP (RFC 9110: PUT is idempotent).

One transaction:
1. `SELECT … FROM events WHERE id = :id FOR UPDATE`. A missing event or a **draft** returns `404 EVENT_NOT_FOUND`.
2. `t = now_utc()` (after the lock).
3. Event `cancelled` → `409 EVENT_CANCELLED`. If the caller's role is `organiser` and they own the event → `403 ORGANISER_CANNOT_REGISTER`. Admins may register for an event they created, as the role matrix specifies.
4. Load the caller's row `FOR UPDATE`. If it's already `confirmed`, `waitlisted` or `checked_in`, **return `200` with the current registration and change nothing.** This is checked *before* the window, so a retry after registration closes still gets its answer.
5. Window: `t < registration_opens_at` → `409 REGISTRATION_NOT_OPEN`. `t ≥ registration_closes_at` → `409 REGISTRATION_CLOSED`.
6. `occupancy = count(confirmed + checked_in)`, `waiting = count(waitlisted)`. Both run after the lock.
7. If `occupancy < capacity` → confirmed. Else if `waiting < waitlist_limit` → waitlisted with `queue_no = nextval('waitlist_queue_seq')`. Else → `409 EVENT_FULL`.
8. Insert a new row, or reset and reuse the cancelled row (cycle rules, §3). Commit. **`201`** with `RegistrationOut` (status, `queue_position` if waitlisted).

**What PUT guarantees, precisely:** no duplicate booking effects, and a retry returns the **current registration** (`201` the first time, `200` afterwards). It does **not** replay the original response byte-for-byte. Also, a delayed retry arriving *after* the student cancelled would register them again. Exact response replay with an `Idempotency-Key` header is in Phase 3.

**Safety net:** `uq_registrations_event_id` makes a second row impossible even if the code were wrong. Under the lock protocol this violation can't occur. If it ever does, §12 maps it to `409 ALREADY_REGISTERED` and logs it as a bug.

---

## 10. Cancellation and waitlist promotion

### Self-cancel: `DELETE /api/v1/events/{event_id}/registrations/me`
1. Event `FOR UPDATE`. Then the caller's row `FOR UPDATE`. `t = now_utc()`.
2. No row → `404 REGISTRATION_NOT_FOUND`. Already `cancelled` → **`200`, unchanged** (repeat-safe). `checked_in` → `409 CANNOT_CANCEL_AFTER_CHECKIN`. `t ≥ starts_at` → `409 EVENT_STARTED`.
3. `waitlisted` → cancelled, `queue_no = NULL`. Nobody is promoted, because no seat was freed.
4. `confirmed` → cancelled, `ticket_version += 1` (old tickets die). Then **promote at most one**, but only if the event is `published` and `t < starts_at`.
5. Commit and return `200` with the registration.

### Organiser removal: `DELETE /api/v1/events/{event_id}/registrations/{registration_id}`
For the event's organiser or an admin. Same steps and rules, with `cancelled_by = actor`. It's useful for duplicate or fake accounts and for students who ask in person.

### Promotion (one shared function: used by cancel, removal and capacity increase)

Promotion locks the event first, then all waitlisted registration rows in `(queue_no, id)` order, then the corresponding user rows in ascending UUID order using `FOR SHARE`. It must **not** filter `u.is_active` before taking user locks: the active-state decision is made after the locks are acquired and rows refreshed. Because a waitlisted user may be inactive, read enough queue rows to find up to `n` eligible users; do not limit the initial query to `n` rows.

Conceptually, promotion performs these steps:
```sql
-- 1. Lock waitlisted rows in deterministic FIFO order (no active-state filter yet).
SELECT r.id, r.user_id, r.queue_no
  FROM registrations r
 WHERE r.event_id = :event_id AND r.status = 'waitlisted'
 ORDER BY r.queue_no, r.id
 FOR UPDATE OF r;

-- 2. After collecting user IDs, lock every corresponding user in UUID order.
SELECT u.id, u.is_active
  FROM users u
 WHERE u.id = ANY(:user_ids)
 ORDER BY u.id
 FOR SHARE;
```
The service checks freshly locked user states, then promotes the first `n` active, eligible users in FIFO order. **Deactivated users and organisers who own this event are skipped** and remain waitlisted. Ticket check-in locks the event and registration first, then the attendee and scanner user rows in UUID order, and re-checks both account activity and scanner authorization. Admin changes lock actor, target and active-admin rows in the same UUID order. These shared rules serialize eligibility changes without introducing a user-row lock inversion.

- **Two cancellations at once:** both need the event's `FOR UPDATE` lock, so they run one after the other. The second promotes the *next* person, never the same one.
- **Promotion runs until the event starts,** even after registration closes. Late cancellations are exactly when a waitlist helps.
- **Limitation:** there are no notifications in Phase 1. A promoted student sees `confirmed` in `GET …/registrations/me` or `GET /me/registrations`.

### Reads
| Endpoint | Purpose |
|---|---|
| `GET /events/{id}/registrations/me` | My status, plus my `queue_position` if waitlisted |
| `GET /me/registrations` | All my registrations (paginated) |
| `GET /events/{id}/registrations` | Organiser or admin: attendees (name, email, status, queue position, check-in time). Filter by `status`, paginated. |

---

## 11. Tickets and check-in

### The ticket design: versioned, signed tickets (HMAC)
A ticket is **built from data and signed**, not stored:
```
payload = registration_id (16 bytes) ‖ ticket_version (4 bytes, big-endian)
token   = "cp1." + base64url(payload) + "." + base64url( HMAC-SHA256(TICKET_SECRET, "cp1." + base64url(payload)) )
```
That's about 75 URL-safe characters, and it's what the QR code contains. The `cp1.` prefix marks the format version and makes leaked tokens easy to recognise.

- **Viewing is safe and repeatable.** The same registration and version always produce the same token, so the JSON view and the QR view show the **same ticket**, and opening it twice never invalidates a screenshot.
- **Invalidation = increase the version.** Reissue does `ticket_version += 1`, and so does cancellation. A token carrying an old version no longer matches the row.
- **Nothing secret is stored in the DB.** A database leak alone can't produce valid tickets. Forging one needs `TICKET_SECRET`, which is a **separate key** from `JWT_SECRET` (key separation: one leaked key shouldn't break both systems). **Hook: different keys for your house and your scooter.**
- Rotating `TICKET_SECRET` invalidates **every** ticket. Students simply re-open their ticket. This is documented as the emergency switch.
- MACs are compared with `hmac.compare_digest` (constant time).

### View the ticket: `GET /api/v1/events/{event_id}/registrations/me/ticket?format=json|png`
- Allowed when the registration is `confirmed`, the event is `published`, and `now < ends_at`. An organiser cannot view a ticket for an event they own (`403 FORBIDDEN`), while admins may attend events they created. Otherwise `409 TICKET_NOT_AVAILABLE`.
- `format=json` (default) returns `{token, event_id, event_title, attendee_name, ticket_version}`. `format=png` returns `image/png`, rendered from the **same token**. One logical ticket, two representations, **no rotation**.
- No locks are taken, because nothing changes. If a cancellation commits a moment later, the token just shown is already dead: check-in re-validates status and version under its locks. This is acceptable by design and tested (§14, test 6).

### Reissue: `POST /api/v1/events/{event_id}/registrations/me/ticket/reissue`
The "my ticket was copied or leaked" button.
1. Event `FOR SHARE`. Own row `FOR UPDATE`. Lock and refresh the caller's user row `FOR SHARE`, then re-check active status, role/ownership, confirmed state, published state and event end time.
2. `ticket_version += 1`. Commit, **then** build and return the new token (rendering happens after commit, Rule 5).
3. **Concurrent reissues are serialized by the row lock.** Each one increases the version, so only the token from the **last** commit is valid. An earlier response's token may already be dead. Clients shouldn't reissue in parallel, and the reissue limit is 5 per hour.
4. The raw token is never logged.

### Check-in: `POST /api/v1/events/{event_id}/checkins` with body `{token}`
Callable by the event's organiser or an admin. The token travels in the **body, never in the URL**, because URLs end up in logs and browser history.

0. Parse and verify the HMAC **before touching the DB**. A malformed or wrongly signed token returns `404 INVALID_TICKET` cheaply.
1. Begin. `SELECT … FROM events WHERE id = :event_id FOR SHARE`. Not visible → `404`. Not the organiser or an admin → `403`.
2. Validate that the locked event is `published` (else `409 EVENT_NOT_ACTIVE`).
3. `SELECT … FROM registrations WHERE id = :rid AND event_id = :event_id FOR UPDATE`. **`event_id` is in the WHERE clause**, so a ticket for event B is invalid at event A. Not found → `404 INVALID_TICKET`.
4. Lock and refresh the attendee and scanner `users` rows with `FOR SHARE`, acquired in ascending UUID order after the registration row. The scanner's row is locked too, so a role change or deactivation that commits first cannot be ignored by a request using stale authentication data.
5. Re-check scanner activity and call the event-ownership/admin permission check with the refreshed scanner; disabled or newly unauthorized scanners receive `403`.
6. `r.ticket_version ≠ token version` → `404 INVALID_TICKET` (an obsolete ticket). `r.status = 'checked_in'` → `409 ALREADY_CHECKED_IN` (with the time). Any other status that isn't `confirmed` → `404 INVALID_TICKET`.
7. Attendee deactivated → `409 ATTENDEE_DISABLED`. If the attendee is now an organiser who owns the event, reject the old ticket as `404 INVALID_TICKET`.
8. **Only now read `t = now_utc()`**, after the event, registration, and user locks are held. Validate `starts_at − 2h ≤ t ≤ ends_at` (else `409 CHECKIN_NOT_OPEN`).
9. `UPDATE … SET status='checked_in', checked_in_at=t, checked_in_by=:actor`. Commit. Return `200 {registration_id, attendee_name, checked_in_at}`.

Because the row is locked in step 3, the check-then-update sequence is safe. **Twenty simultaneous scans of one ticket:** all share the event lock, queue on the registration row, and the first one wins. The others then see `checked_in` and get `409`.

**Check-in vs event cancellation:** check-in holds `FOR SHARE` and cancellation needs `FOR UPDATE`, so one waits for the other:
- If check-in goes first, it succeeds while the event is still published, and the cancellation commits afterwards. That's valid: the student really was admitted before the cancellation.
- If cancellation goes first, the waiting check-in then reads `cancelled` and fails with `409 EVENT_NOT_ACTIVE`.

**Invariant:** no registration has `checked_in_at > event.cancelled_at`.

### A QR ticket is a bearer credential
Whoever holds the token can be admitted, just like a cinema ticket. That's why check-in returns the **attendee's name** so the volunteer can compare it with the student's **college ID**. That's an operational control at the door, separate from the cryptographic one. A student who suspects a copy can reissue, which invalidates the copy.

---

## 12. Errors, DB exceptions and observability

### Error shape (every non-2xx response)
```json
{ "error": { "code": "EVENT_FULL", "message": "This event and its waitlist are full.", "request_id": "6f1c…" } }
```
422 responses add `"details": [{"field": "...", "issue": "..."}]`. Handlers cover `AppError`, `HTTPException`, `RequestValidationError`, database errors (below) and a catch-all `Exception` (500 `INTERNAL_ERROR`, never a stack trace, SQL, secret or token).

### Database exception mapping (precise, never a blanket rule)
The `get_db()` dependency **rolls back** the session before any error response is built. Then:

| Database error (psycopg `sqlstate`, `diag.constraint_name`) | API response | Log level |
|---|---|---|
| 23505 unique, `uq_users_email` | 409 `EMAIL_TAKEN` | info |
| 23505 unique, `uq_registrations_event_id` | 409 `ALREADY_REGISTERED` | **error** (it shouldn't happen under the lock protocol) |
| 23514 check, `ck_events_time_order` | 422 `INVALID_EVENT_TIMES` (defence in depth; the service validates first) | warning |
| Any other 23xxx (unknown constraint) | 500 `INTERNAL_ERROR` | **error**, a bug |
| 55P03 lock_not_available (`lock_timeout`) | 503 `BUSY` + `Retry-After: 1` | warning |
| 57014 query_canceled (`statement_timeout`) | 503 `BUSY` + `Retry-After: 1` | warning |
| 40P01 deadlock_detected | 503 `BUSY` | **error** (the lock order was violated somewhere) |
| Connection or `OperationalError` | 503 `DATABASE_UNAVAILABLE` | error |
| Anything else | 500 `INTERNAL_ERROR` | error |

An outage or a programming error **never** becomes a business conflict like `ALREADY_REGISTERED`.

### Status codes
| Code | Meaning in CampusPass |
|---|---|
| 200 | Reads. Actions returning a body (cancel, check-in, PUT when already registered). |
| 201 | Signup, event created, registration created or reactivated |
| 204 | Draft deleted |
| 401 | No, bad or expired token. Wrong credentials. |
| 403 | Authenticated but not allowed (role, ownership), disabled account, organiser registering for their own event |
| 404 | Not found, someone else's draft, invalid ticket |
| 409 | Business conflict: `EVENT_FULL`, `EVENT_CANCELLED`, `EVENT_NOT_ACTIVE`, `EVENT_STARTED`, `EVENT_ENDED`, `REGISTRATION_NOT_OPEN`, `REGISTRATION_CLOSED`, `ALREADY_CHECKED_IN`, `CANNOT_CANCEL_AFTER_CHECKIN`, `CHECKIN_NOT_OPEN`, `TICKET_NOT_AVAILABLE`, `ATTENDEE_DISABLED`, `CAPACITY_BELOW_OCCUPANCY`, `EMAIL_TAKEN`, `CANNOT_MODIFY_SELF`, `LAST_ADMIN`, `INVALID_STATE` |
| 411 / 413 | Body without a length / body too large |
| 422 | Validation (extra fields, timestamps without a timezone, password policy, bad query parameters) |
| 429 | Rate limited (with `Retry-After`) |
| 503 | `BUSY` (lock or statement timeout), `DATABASE_UNAVAILABLE` |

### Observability
- **Request ID middleware:** uses the incoming `X-Request-ID` if it's a valid UUID, otherwise generates one. It's returned in the header and included in every log line and error body.
- **Structured JSON logs** to stdout: time, level, request_id, method, **route template** (for example `/events/{event_id}/checkins`), status, duration_ms, user_id.
- **Never logged:** the `Authorization` header, request bodies, passwords, JWTs, ticket tokens, secrets, database URLs.
- `GET /health` (liveness) returns `{"status":"ok"}` with no DB call. `GET /health/ready` returns `{"status":"ok","db":"ok","migration":"<alembic revision>"}`, with `503` if the DB is unreachable. Neither shows credentials.

---

## 13. Security checklist (OWASP API Security Top 10, 2023)

| Risk | Control in CampusPass |
|---|---|
| API1 Broken Object Level Authorization | Ownership checks on every event and registration route. IDs come from the token. Drafts return 404. Check-in matches `event_id`. |
| API2 Broken Authentication | Argon2id + NIST password policy. Pinned HS256 with required `exp/iat/sub/iss/aud/jti`. Generic login error + dummy-hash timing. Per-IP and per-email login limits. 30-minute tokens. JSON login (no ROPC). |
| API3 Broken Object Property Level Authorization | `extra="forbid"` on every input. Explicit output models without hashes or secrets. |
| API4 Unrestricted Resource Consumption | Body limit 64 KiB, `page_size` ≤ 100, string caps, password ≤ 128, rate limits, lock and statement timeouts |
| API5 Broken Function Level Authorization | Role dependencies. Role read from the DB on every request. Last-admin and self guards. |
| API6 Unrestricted Access to Sensitive Business Flows | Registration and cancellation limits per account and IP, optional email-domain allowlist, organiser removal. Multi-account hoarding is documented as a residual risk (Phase 3: verified email). |
| API7 SSRF | No outbound requests are made |
| API8 Security Misconfiguration | Secrets only from env, validated at startup. `.env` git-ignored. No stack traces. Trusted-proxy config only in deployment. CORS off (no browser client). HTTPS terminated by the hosting platform. |
| API9 Improper Inventory Management | One versioned base path `/api/v1`. The OpenAPI document is the inventory. |
| API10 Unsafe Consumption of APIs | No third-party APIs are consumed |

---

## 14. Testing

### Test database safety guard (runs before anything destructive)
Every one of these must pass, or the run **stops** with a clear message:
1. `TEST_DATABASE_URL` is set. There is **no fallback** to `DATABASE_URL`.
2. `make_url(TEST_DATABASE_URL).database == "campuspass_test"` exactly.
3. The test URL's host, port and database differ from `DATABASE_URL`'s.
4. After connecting: `SELECT current_database()` returns `campuspass_test`.
5. Any error while verifying = **fail closed**.

`test_db_guard.py` proves that the guard rejects the application DB URL.

### Schema in tests
A session-scoped fixture runs **`alembic upgrade head`** on the test DB, which proves the migrations work. Between tests, `TRUNCATE registrations, events, users` (sequence gaps are fine). `create_all()` is never used.

### Live-server concurrency harness
- A fixture starts **uvicorn in a background thread** on a free port, against the test DB, with `RATE_LIMITS_ENABLED=false`.
- Each worker thread has its **own `httpx.Client`**, so the server gives each request its own DB session.
- A `threading.Barrier(n)` releases all threads at once, so requests really collide.
- **Assertions check the final database state** through a separate session, not only the HTTP status counts.
- Concurrency tests are marked `@pytest.mark.concurrency` and repeated (for example 5 rounds) to catch timing-dependent bugs.

### Test catalogue
**Functional and auth**
- Signup (valid, `role` in body gives 422, duplicate email gives 409, password under 15 or on the blocklist gives 422, a domain outside the allowlist gives 422 when configured).
- Login (same 401 for an unknown email and a wrong password, disabled account gives 403).
- Tokens (expired, malformed, `alg:none`, wrong `iss`/`aud`, missing `jti`, `sub` not a UUID, all give 401). Deactivated user's token gives 401. A role change applies to the very next request.

**Permissions** (and **the data is unchanged after every denial**)
- A student creating an event gets 403.
- Organiser A editing B's event gets 403.
- An outsider reading a draft gets 404.
- A student hitting admin routes gets 403.
- B's organiser checking in at A's event gets 403.
- A student removing someone else's registration gets 403.

**Business rules** (using the fixed test clock)
- Registration window edges: just before opening, exactly at close.
- A cancelled event rejects registration.
- PUT repeated → 201 then 200, with one row.
- Re-registration goes to the back of the queue.
- Promotion skips inactive users.
- Capacity decrease below occupancy → 409, with nothing changed.
- Capacity increase promotes in FIFO order.
- Cancelling after check-in → 409.
- Event edits after start → 409.
- Deleting a published event → 409.
- Self-demotion → 409.

**Required concurrency, integrity, and release-regression tests** (database tests run against PostgreSQL with separate sessions)

| # | Test | Pass condition (checked in the DB) |
|---|---|---|
| 1 | 50 students, capacity 10, waitlist 40 | Exactly 10 confirmed, 40 waitlisted. Unique `queue_no`s. Positions 1–40 with no gaps. |
| 2 | 50 simultaneous PUTs from one student | Exactly 1 row and 1 confirmed. Responses: one 201, 49 × 200. |
| 3 | 5 concurrent cancellations with 10 waitlisted | Exactly the 5 lowest `queue_no`s promoted. Occupancy = capacity. |
| 4 | 20 simultaneous scans of one ticket | One 200 and 19 × 409. `checked_in_at` set once. |
| 5 | Event cancellation racing check-in (repeated) | Each round: either check-in 200 with `checked_in_at ≤ cancelled_at`, or check-in 409 `EVENT_NOT_ACTIVE` with the registration still `confirmed`. **Never** a check-in after the cancellation. |
| 6 | Ticket view or reissue racing self-cancellation | Final status `cancelled`. Every token obtained (before or during) → `404 INVALID_TICKET` at check-in. |
| 7 | 5 concurrent reissues | Final `ticket_version` = start + 5. Only the token with the final version checks in. All others → 404. |
| 8 | Capacity increase racing registrations | Occupancy ≤ final capacity. Promotions in FIFO order. |
| 9 | Capacity decrease below occupancy | 409, and the row is unchanged |
| 10 | State constraints (direct SQL) | A non-waitlisted row with `queue_no`, `checked_in` without `checked_in_by`, `cancelled` without `cancelled_at` or `cancelled_by`, and a padded or upper-case email are **all rejected by the named constraint** |
| 11 | Rate limits | Each limit in §6 triggers at the right count, **for the right key** (two accounts on one IP are counted separately for account limits). `Retry-After` is present. |
| 12 | Test DB guard | Refuses `campuspass`, accepts `campuspass_test` |
| 13 | DB error mapping | A forced duplicate email → 409 `EMAIL_TAKEN`. An event lock held in another connection → 503 `BUSY` within `lock_timeout`. An unknown constraint violation → 500, **not** `ALREADY_REGISTERED`. |
| 14 | Last admin (two admins disable each other at once) | At least one active admin remains |
| 15 | User deactivation/role change racing with promotion/check-in/admin operation | If the identity change commits first, stale permission or inactive status is not used; final rows are consistent |
| 16 | Check-in waits past its deadline on a registration lock | Returns `409 CHECKIN_NOT_OPEN`; registration remains `confirmed` and unchecked |
| 17 | Pool timeout and malformed ticket handling | Pool timeout becomes 503 `BUSY`; malformed/noncanonical tokens are rejected safely |
| 18 | Demo seeding and OpenAPI contract | Repeat seeding preserves unknown credentials; explicit reset is development-only; exported paths/schema match the running app |
| 19 | Role transition versus attendee ticket | Admins may attend their own events; organisers may not attend their own, even with a registration created before a role change |

**Recording results:** the README quotes the exact command (`pytest -v`, `pytest -m concurrency -v`) and its observed summary line. Nothing is claimed before it has been seen to pass.

### Load report (Phase 2, kept separate from correctness)
`scripts/load_test.py` reports: setup (CPU, RAM, PostgreSQL version, workers), request count, concurrency, confirmed, waitlisted, rejected, unexpected errors, maximum observed occupancy, and p50/p95/p99 latency. A correctness test proving "10 of 10 seats" is **not** a throughput claim.

---

## 15. Configuration and repo hygiene

### `.env.example` (committed, placeholders only)
```
APP_ENV=development
DATABASE_URL=postgresql+psycopg://campuspass:CHANGE_ME@localhost:5432/campuspass
TEST_DATABASE_URL=postgresql+psycopg://campuspass:CHANGE_ME@localhost:5432/campuspass_test
JWT_SECRET=CHANGE_ME_run_python_-c_import_secrets;print(secrets.token_urlsafe(48))
JWT_ISSUER=campuspass
JWT_AUDIENCE=campuspass-api
ACCESS_TOKEN_MINUTES=30
TICKET_SECRET=CHANGE_ME_a_different_token_urlsafe_48_value
RATE_LIMITS_ENABLED=true
ALLOWED_EMAIL_DOMAINS=
MAX_BODY_BYTES=65536
DB_LOCK_TIMEOUT_MS=3000
DB_STATEMENT_TIMEOUT_MS=10000
```

### Startup validation (the app refuses to start if any check fails)
- Both secrets are at least 64 characters, are not placeholders, and differ from each other.
- `APP_ENV` ∈ {development, test, production}.
- `RATE_LIMITS_ENABLED=false` only when `APP_ENV=test`.
- `seed_demo` refuses to run when `APP_ENV=production`.

### `.gitignore`
`.env`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.vscode/`, `*.png`, `*.log`

### Migrations
- Alembic from day one. `alembic upgrade head` is a **documented manual step**. The app never creates or drops tables at startup.
- After `alembic revision --autogenerate`, the generated file is **reviewed by hand**. Autogenerate doesn't always render everything (check enum types, the standalone `waitlist_queue_seq` sequence, CHECK constraints and the partial index), and missing pieces are added with `op.execute(...)`.
- Phase 2: a CI job runs `upgrade head → downgrade base → upgrade head` to prove migrations can be reversed.

---

## 16. Windows setup

Use PowerShell or the VS Code terminal, inside the project folder.

### 16.1 Check your tools
```powershell
py -0p            # 3.12 must be listed
git --version
psql --version    # 16, 17 or 18 recommended (see §1). If "not recognized", use the full path in 16.3.
```

### 16.2 Virtual environment
```powershell
mkdir campuspass; cd campuspass; git init
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python --version                      # must print 3.12.x
python -m pip install --upgrade pip
```

### 16.3 Database user and databases
```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres     # use your version folder
```
```sql
CREATE USER campuspass WITH PASSWORD 'choose-a-long-password';
CREATE DATABASE campuspass      OWNER campuspass;
CREATE DATABASE campuspass_test OWNER campuspass;
\q
```
- The app connects as `campuspass`, never as the `postgres` superuser (least privilege).
- Avoid `@ : / # %` in the DB password, or URL-encode them, because the password sits inside `DATABASE_URL`.
- If the connection fails, open `services.msc` and check that **postgresql-x64-NN** is Running.

### 16.4 Secrets and `.env`
```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # run twice: JWT_SECRET, TICKET_SECRET
notepad .env
```

### 16.5 Install (locked), migrate, run
```powershell
pip install -r requirements-dev.txt     # the exact pinned versions (§17)
alembic upgrade head                    # creates the tables
python -m scripts.create_admin
python -m scripts.seed_demo             # optional demo data
uvicorn app.main:app --reload           # http://127.0.0.1:8000/docs
```

### 16.6 Everyday commands
| Task | Command |
|---|---|
| All tests | `pytest -v` |
| Concurrency proof only | `pytest -m concurrency -v` |
| New migration | `alembic revision --autogenerate -m "describe change"`, then **review it**, then `alembic upgrade head` |
| Inspect the DB | `psql -U campuspass -d campuspass`, then `\dt` and `\d registrations` |
| Re-lock dependencies | see §17 |

---

## 17. Dependencies and locking

### Direct dependencies: `requirements.in`
```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy>=2.0.30,<3
alembic>=1.13
psycopg[binary]>=3.2
pydantic>=2.7,<3
pydantic-settings>=2.3
email-validator>=2.1
pyjwt>=2.8,<3
pwdlib[argon2]>=0.2.1
limits>=3.13
qrcode[pil]>=7.4
```

### Dev dependencies: `requirements-dev.in`
```
-r requirements.in
pytest>=8.2
httpx>=0.27
```

### The one locking process
1. Install the maintainer tool once inside the venv: `pip install uv`.
2. Compile **cross-platform** lock files (pinned versions plus all sub-dependencies):
   ```powershell
   uv pip compile requirements.in     --universal --python-version 3.12 -o requirements.txt
   uv pip compile requirements-dev.in --universal --python-version 3.12 -o requirements-dev.txt
   ```
3. Install from the lock: `pip install -r requirements-dev.txt`.
4. Run `pytest -v`. It must pass.
5. Commit **all four files**.
6. On any dependency change: edit the `.in` file, then repeat steps 2–5.

**Why not `pip freeze`?** It records what's installed on *this* machine and OS. A Windows freeze can miss packages that only Linux needs (for example `uvloop`, which `uvicorn[standard]` installs only on non-Windows), so CI on Linux could break. `--universal` resolves for all platforms at once. Reviewers don't need `uv`, because plain `pip install -r requirements-dev.txt` works.

| Package | Why it's needed |
|---|---|
| fastapi, uvicorn | Framework and server |
| sqlalchemy, alembic | ORM with `with_for_update(read=…)`, and migrations |
| psycopg[binary] | PostgreSQL driver, with `sqlstate` and `diag` for error mapping |
| pydantic, pydantic-settings, email-validator | Validation and config (`EmailStr` needs email-validator) |
| pyjwt | Access tokens |
| pwdlib[argon2] | Argon2id |
| limits | Rate limiting with explicit keys |
| qrcode[pil] | QR PNG |
| pytest, httpx | Tests and the live-server clients |

(`python-multipart` from v2.1 is **removed**, because there's no form login any more.)

---

## 18. File tree (★ = files to know best)

```
campuspass/
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_schema.py       ★ initial schema, enums, sequence, CHECKs, partial index
│       └── 0002_require_cancelled_by.py strengthens the cancelled-row actor constraint
├── alembic.ini
├── app/
│   ├── main.py                          app factory, middleware, exception handlers, routers
│   ├── core/
│   │   ├── config.py                    settings and startup validation
│   │   ├── database.py                  engine, pool, lock/statement timeouts, sessions
│   │   ├── naming.py                    deterministic constraint/index names
│   │   ├── clock.py                     now_utc() business clock
│   │   ├── locks.py                   ★ event, registration, and user-row lock helpers
│   │   ├── security.py                ★ Argon2id + JWT creation/validation
│   │   ├── tickets.py                 ★ versioned HMAC ticket generation/validation
│   │   ├── deps.py                      HTTP Bearer, current user, roles
│   │   ├── ratelimit.py                 explicit-key rate limiting
│   │   ├── errors.py                    consistent API errors and exception handlers
│   │   ├── db_errors.py                 SQLSTATE/constraint/pool-timeout mapping
│   │   └── middleware.py                request IDs, body size limits, JSON access logging
│   ├── data/common_passwords.txt        password blocklist
│   ├── models/                          users, events, registrations, enums, Base
│   ├── schemas/                         Pydantic request and response models
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── admin_service.py
│   │   ├── event_service.py
│   │   ├── promotion.py               ★ FIFO promotion with user eligibility locks
│   │   ├── registration_service.py   ★ registration and cancellation transactions
│   │   └── ticket_service.py         ★ ticket issuance, reissue and check-in
│   └── routers/                         thin HTTP routes
├── docs/
│   ├── DECISIONS.md
│   ├── LEARNING_GUIDE.md
│   ├── THIRD_PARTY_NOTICES.md
│   ├── campuspass_spec_v3.0.md
│   └── openapi.json                     generated API contract
├── licenses/SECLISTS-MIT.txt
├── scripts/
│   ├── create_admin.py
│   ├── seed_demo.py
│   └── export_openapi.py
├── tests/
│   ├── conftest.py                      test DB guard, migrations, fixtures, fixed clock
│   ├── live_server.py                   live-server/thread-barrier helper
│   ├── test_auth.py
│   ├── test_admin.py
│   ├── test_events.py
│   ├── test_permissions.py
│   ├── test_registrations.py
│   ├── test_tickets.py
│   ├── test_concurrency.py             ★ race and final-state assertions
│   ├── test_constraints.py
│   ├── test_db_errors.py
│   ├── test_rate_limits.py
│   ├── test_seed_demo.py
│   └── test_openapi_contract.py
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.in
├── requirements-dev.in
├── requirements.txt                    generated from requirements.in when lock resolution is available
├── requirements-dev.txt                generated from requirements-dev.in when lock resolution is available
└── README.md
```

The current update archive does not yet contain the two generated `requirements*.txt` lock files because package-index access was unavailable during the update. They must be generated and committed on a network-connected machine before installation is reproducible.

---

## 19. API reference (base path `/api/v1`, except health)

🔓 no login · 🔑 any logged-in user · 🧑‍💼 event owner or admin · 🛡️ admin

### Auth
| Method | Path | Who | Body | Success | Errors |
|---|---|---|---|---|---|
| POST | `/auth/register` | 🔓 | `SignupIn` | 201 `UserOut` | 409 EMAIL_TAKEN, 422, 429 |
| POST | `/auth/login` | 🔓 | `LoginIn` (JSON) | 200 `TokenOut` | 401, 403 ACCOUNT_DISABLED, 429 |
| GET | `/auth/me` | 🔑 | – | 200 `UserOut` | 401 |

### Events
| Method | Path | Who | Success | Errors |
|---|---|---|---|---|
| GET | `/events` | 🔓 | 200 `Page[EventOut]` | 422, 429 |
| GET | `/events/{id}` | 🔓 / 🧑‍💼 for drafts | 200 `EventOut` | 404 |
| POST | `/events` | organiser, admin | 201 `EventOut` | 403, 422 |
| PATCH | `/events/{id}` | 🧑‍💼 | 200 `EventOut` | 403, 404, 409, 422, 503 |
| POST | `/events/{id}/publish` | 🧑‍💼 | 200 `EventOut` | 409 INVALID_STATE / REGISTRATION_CLOSED |
| POST | `/events/{id}/cancel` | 🧑‍💼 | 200 `EventOut` | 409 INVALID_STATE / EVENT_ENDED |
| DELETE | `/events/{id}` | 🧑‍💼 | 204 | 409 INVALID_STATE |

### Registrations
| Method | Path | Who | Success | Errors |
|---|---|---|---|---|
| PUT | `/events/{id}/registrations/me` | 🔑 | 201 created / 200 already registered, `RegistrationOut` | 403, 404, 409 EVENT_FULL / EVENT_CANCELLED / REGISTRATION_NOT_OPEN / REGISTRATION_CLOSED, 429, 503 |
| GET | `/events/{id}/registrations/me` | 🔑 | 200 `RegistrationOut` | 404 |
| DELETE | `/events/{id}/registrations/me` | 🔑 | 200 `RegistrationOut` | 404, 409 EVENT_STARTED / CANNOT_CANCEL_AFTER_CHECKIN, 429 |
| GET | `/events/{id}/registrations` | 🧑‍💼 | 200 `Page[AttendeeOut]` | 403, 404 |
| DELETE | `/events/{id}/registrations/{registration_id}` | 🧑‍💼 | 200 `RegistrationOut` | 403, 404, 409 |

### Tickets and check-in
| Method | Path | Who | Success | Errors |
|---|---|---|---|---|
| GET | `/events/{id}/registrations/me/ticket?format=json\|png` | 🔑 | 200 `TicketOut` or `image/png` | 404, 409 TICKET_NOT_AVAILABLE, 429 |
| POST | `/events/{id}/registrations/me/ticket/reissue` | 🔑 | 200 `TicketOut` | 409 TICKET_NOT_AVAILABLE, 429 |
| POST | `/events/{id}/checkins` | 🧑‍💼 | 200 `CheckinOut` | 403, 404 INVALID_TICKET, 409 ALREADY_CHECKED_IN / EVENT_NOT_ACTIVE / CHECKIN_NOT_OPEN / ATTENDEE_DISABLED, 429 |

### Me, admin, health
| Method | Path | Who | Success |
|---|---|---|---|
| GET | `/me/registrations` | 🔑 | 200 `Page[RegistrationOut]` |
| GET | `/me/events` | organiser, admin | 200 `Page[EventOut]` |
| GET | `/admin/users` | 🛡️ | 200 `Page[UserOut]` |
| PATCH | `/admin/users/{id}` | 🛡️ | 200 `UserOut` (409 CANNOT_MODIFY_SELF / LAST_ADMIN) |
| GET | `/health` · `/health/ready` | 🔓 | 200 (`ready` gives 503 if the DB is down) |

### Schemas
| Schema | Fields |
|---|---|
| `SignupIn` | email, password (15–128, policy), full_name (1–100). extra=forbid. |
| `LoginIn` | email, password. extra=forbid. |
| `UserOut` | id, email, full_name, role, is_active, created_at |
| `TokenOut` | access_token, token_type="bearer", expires_in |
| `EventCreate` | title, description?, category, venue, registration_opens_at, registration_closes_at, starts_at, ends_at (AwareDatetime), capacity, waitlist_limit?. extra=forbid. |
| `EventUpdate` | the same fields, all optional, extra=forbid (no status or organiser_id) |
| `EventOut` | the event fields + organiser_name + is_completed, is_registration_open, seats_left, waitlist_count (display snapshots) |
| `RegistrationOut` | id, event_id, event_title, event_status, status, queue_position?, confirmed_at, waitlisted_at, cancelled_at, checked_in_at |
| `AttendeeOut` | registration_id, user_id, full_name, email, status, queue_position?, checked_in_at |
| `TicketOut` | token, event_id, event_title, attendee_name, ticket_version |
| `CheckinIn` | token (≤ 128 characters). extra=forbid. |
| `CheckinOut` | registration_id, attendee_name, checked_in_at |
| `AdminUserUpdate` | role?, is_active? (at least one). extra=forbid. |
| `Page[T]` | items, page, page_size, total |

---

## 20. Reference SQL (what Alembic's initial migration must produce)

```sql
CREATE TYPE user_role           AS ENUM ('student','organiser','admin');
CREATE TYPE event_category      AS ENUM ('tech','cultural','sports','workshop','other');
CREATE TYPE event_status        AS ENUM ('draft','published','cancelled');
CREATE TYPE registration_status AS ENUM ('confirmed','waitlisted','cancelled','checked_in');
CREATE SEQUENCE waitlist_queue_seq;

CREATE TABLE users (
    id            uuid PRIMARY KEY,
    email         varchar(254) NOT NULL,
    password_hash text         NOT NULL,
    full_name     varchar(100) NOT NULL,
    role          user_role    NOT NULL DEFAULT 'student',
    is_active     boolean      NOT NULL DEFAULT true,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT ck_users_email_normalized CHECK (email = lower(btrim(email)))
);

CREATE TABLE events (
    id                     uuid PRIMARY KEY,
    organiser_id           uuid           NOT NULL REFERENCES users(id),
    title                  varchar(150)   NOT NULL,
    description            text           NOT NULL DEFAULT '',
    category               event_category NOT NULL,
    venue                  varchar(200)   NOT NULL,
    registration_opens_at  timestamptz    NOT NULL,
    registration_closes_at timestamptz    NOT NULL,
    starts_at              timestamptz    NOT NULL,
    ends_at                timestamptz    NOT NULL,
    capacity               integer        NOT NULL,
    waitlist_limit         integer        NOT NULL DEFAULT 50,
    status                 event_status   NOT NULL DEFAULT 'draft',
    cancelled_at           timestamptz,
    created_at             timestamptz    NOT NULL DEFAULT now(),
    updated_at             timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT ck_events_capacity       CHECK (capacity BETWEEN 1 AND 10000),
    CONSTRAINT ck_events_waitlist_limit CHECK (waitlist_limit BETWEEN 0 AND 10000),
    CONSTRAINT ck_events_description    CHECK (char_length(description) <= 5000),
    CONSTRAINT ck_events_time_order     CHECK (registration_opens_at < registration_closes_at
                                           AND registration_closes_at <= starts_at
                                           AND starts_at < ends_at),
    CONSTRAINT ck_events_cancelled_at   CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);
CREATE INDEX ix_events_status_starts_at ON events (status, starts_at);
CREATE INDEX ix_events_category         ON events (category);
CREATE INDEX ix_events_organiser_id     ON events (organiser_id);

CREATE TABLE registrations (
    id             uuid PRIMARY KEY,
    event_id       uuid                NOT NULL REFERENCES events(id),
    user_id        uuid                NOT NULL REFERENCES users(id),
    status         registration_status NOT NULL,
    queue_no       bigint,
    ticket_version integer             NOT NULL DEFAULT 1,
    confirmed_at   timestamptz,
    waitlisted_at  timestamptz,
    cancelled_at   timestamptz,
    cancelled_by   uuid REFERENCES users(id),
    checked_in_at  timestamptz,
    checked_in_by  uuid REFERENCES users(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_registrations_event_id UNIQUE (event_id, user_id),
    CONSTRAINT ck_registrations_waitlist_fields CHECK (
        (status = 'waitlisted' AND queue_no IS NOT NULL AND waitlisted_at IS NOT NULL)
        OR (status <> 'waitlisted' AND queue_no IS NULL)),
    CONSTRAINT ck_registrations_confirmed_fields CHECK (
        status NOT IN ('confirmed','checked_in') OR confirmed_at IS NOT NULL),
    CONSTRAINT ck_registrations_checkin_fields CHECK (
        (status = 'checked_in' AND checked_in_at IS NOT NULL AND checked_in_by IS NOT NULL)
        OR (status <> 'checked_in' AND checked_in_at IS NULL AND checked_in_by IS NULL)),
    CONSTRAINT ck_registrations_cancel_fields CHECK (
        (status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)),
    CONSTRAINT ck_registrations_ticket_version CHECK (ticket_version >= 1)
);
CREATE INDEX ix_registrations_event_status ON registrations (event_id, status);
CREATE INDEX ix_registrations_user_id      ON registrations (user_id);
CREATE INDEX ix_registrations_waitlist     ON registrations (event_id, queue_no)
    WHERE status = 'waitlisted';
```
Foreign keys use the default **RESTRICT**: history is never silently deleted. Users are deactivated, not deleted.

### The queries to know by heart
```sql
-- Registration / cancellation / edits: exclusive event lock, then decide
SELECT * FROM events WHERE id = :event_id FOR UPDATE;
SELECT count(*) FILTER (WHERE status IN ('confirmed','checked_in')) AS occupancy,
       count(*) FILTER (WHERE status = 'waitlisted')                AS waiting
  FROM registrations WHERE event_id = :event_id;

-- Check-in / reissue: event first, then registration, then user rows
SELECT * FROM events WHERE id = :event_id FOR SHARE;
SELECT r.* FROM registrations r
 WHERE r.id = :registration_id AND r.event_id = :event_id
 FOR UPDATE OF r;
-- After registration is locked, fetch scanner and attendee IDs and lock them in UUID order:
SELECT u.id, u.role, u.is_active, u.full_name FROM users u
 WHERE u.id = ANY(:scanner_and_attendee_ids)
 ORDER BY u.id FOR SHARE;

-- Promotion (event already locked FOR UPDATE): do not filter by users.is_active yet
SELECT r.id, r.user_id, r.queue_no FROM registrations r
 WHERE r.event_id = :event_id AND r.status = 'waitlisted'
 ORDER BY r.queue_no, r.id FOR UPDATE OF r;
-- Then lock all corresponding users in UUID order and check is_active AFTER locking:
SELECT u.id, u.is_active FROM users u
 WHERE u.id = ANY(:waitlisted_user_ids)
 ORDER BY u.id FOR SHARE;
```

---

## 21. Implementation plan (no deadline: each step finishes with its tests green, then a commit)

| Step | Build | Done when | Commit |
|---|---|---|---|
| 1 | Setup (§16), lock files (§17), config + startup validation, DB engine with timeouts, naming convention, models, **Alembic migrations 0001 and 0002** (reviewed), health endpoints, test DB guard + guard test | `alembic upgrade head` creates the schema and applies migration 0002 for the cancellation-actor constraint, `\d registrations` shows every constraint, `/health/ready` is ok, guard test green | `chore: skeleton, config, schema migration, db guard` |
| 2 | Error shape, request-ID + body-limit + log middleware, `db_errors` mapping | Handler unit tests green | `feat: error handling and middleware` |
| 3 | Auth: password policy, Argon2id, JSON login, JWT (claims), HTTPBearer, roles, admin endpoints + guards, `create_admin` | `test_auth`, `test_admin` green | `feat: auth, roles, admin` |
| 4 | Rate-limit dependency, all §6 limits | `test_rate_limits` green | `feat: keyed rate limiting` |
| 5 | Events: CRUD, publish, cancel, delete, listing (single aggregate query), PATCH rules | `test_events`, `test_permissions` (events) green | `feat: events` |
| 6 | Locks module, promotion, PUT register, self-cancel, organiser removal, reads, `seed_demo` | `test_registrations` + concurrency tests 1, 2, 3, 8, 9 green | `feat: lock protocol, registration, FIFO waitlist` |
| 7 | Tickets (HMAC), view (json/png), reissue, check-in | `test_tickets` + concurrency tests 4, 5, 6, 7 green | `feat: versioned tickets and check-in` |
| 8 | Constraint tests, DB-error tests, last-admin race, full permission matrix | Entire `pytest -v` green, repeated 3 times | `test: integrity and race suite` |
| 9 | README (§22) with **observed** test output, final lock-file check, public repo checked in incognito | §25 checklist fully ticked | `docs: README` |
| P2 | Audit log, CI (Postgres service + migration round-trip + tests), Docker Compose, load report, deployment with trusted proxy | Each item tested | separate commits |

---

## 22. README outline
1. **CampusPass**: a one-line pitch and stack badges
2. **The problem**: overbooked fests, manual waitlists, paper check-in lists
3. **What's implemented** (only tested features), grouped by area
4. **The guarantees**:
   - *Capacity invariant:* `confirmed + checked_in ≤ capacity` at every commit
   - The **lock protocol** table (event first; FOR UPDATE vs FOR SHARE)
   - How row locks (occupancy) and constraints (row consistency, one row per student) work together
   - FIFO waitlist via `queue_no`, with sequence gaps explained
   - PUT semantics: no duplicate effects, but not full response replay
   - Tickets: versioned HMAC, bearer credential + name/ID check
5. **Run it locally** (Windows commands; macOS/Linux differ only in venv activation)
6. **Tests**: exact commands and the **observed** summary lines, plus the date run
7. **Environment used**: Python 3.12.x, PostgreSQL major.minor, OS, and "dependencies installed from `requirements-dev.txt`"
8. **API overview** + link to `/docs`
9. **Design decisions** (6–8 bullets, each with its reason)
10. **Limitations**: in-memory single-process rate limits, no token revocation (30-minute window), no notifications, no email verification (multi-account hoarding), no audit history until Phase 2
11. **Roadmap**: Phase 2 and Phase 3
12. **Project structure**

---

## 23. Interview demo script (about 6 minutes)
1. **Pitch:** *"CampusPass is an event registration API that can't overbook, even when 50 requests hit 10 seats at once. Freed seats go to a fair waitlist in the same transaction, and every ticket works exactly once, provably, with tests against real PostgreSQL."*
2. `/docs`: the endpoint groups. Log in through `/auth/login`, paste the token into **Authorize**. *"Plain bearer tokens. I deliberately avoided the OAuth password grant, because RFC 9700 forbids it."*
3. Signup with `"role":"admin"` → **422**.
4. `seed_demo` event (capacity 2, waitlist 2). Students 1–3 call PUT → confirmed, confirmed, **waitlisted #1**. Student 1 repeats the PUT → **200, same registration**: *"PUT, so retries are safe."*
5. Student 1 cancels → student 3 is **confirmed**, in the same transaction.
6. Student 3 views the ticket as JSON, then as PNG: **the same token**. Organiser checks in → **200 with the name**. Same token again → **409**. *"The QR is a bearer credential. The name lets the volunteer match the college ID."*
7. Reissue for student 2 → the old token gives **404**.
8. Cancel the event → a check-in attempt gives **409 EVENT_NOT_ACTIVE**. Explain FOR SHARE vs FOR UPDATE and the single lock order.
9. `pytest -m concurrency -v` → show the green run. Open `locks.py` and `registration_service.py`: **lock → read clock → count → decide → commit**.

---

## 24. Cross-question prep

| Question | Answer |
|---|---|
| What's the race condition? | Two requests read "1 seat left" before either writes. **Hook: two hands grabbing the last samosa.** |
| How do you prevent it? | Lock the event row (`FOR UPDATE`) before counting. Registrations for that event take turns, and each one sees the previous booking. |
| Why count *after* the lock? | In READ COMMITTED, each statement sees data committed before *it* started. The count after the wait includes the previous holder's booking. **Hook: lock the door, then count the chairs.** |
| Why read the clock after the lock? | SQL `now()` is the transaction **start** time. A request that waited past the deadline would otherwise be accepted. |
| FOR UPDATE vs FOR SHARE? | UPDATE: one holder, for changing the event or its occupancy. SHARE: many holders, for check-ins that need the event to stay published. **Hook: notice board, many readers or one writer.** |
| Can it deadlock? | Every transaction locks the event first, then registrations, and never upgrades SHARE to UPDATE. Consistent order means no cycles. A detected deadlock is logged as a bug and returns 503. |
| What if a lock is stuck? | `lock_timeout` 3s → 503 BUSY with Retry-After. Nobody waits forever. |
| Why not SERIALIZABLE? | Correct, but conflicting transactions fail and need retries. Locks make them wait instead. |
| What do constraints add if you have locks? | Locks protect totals (occupancy). Constraints protect each row (one per student, consistent fields). Defence in depth: even buggy code can't write an impossible row. |
| How is the waitlist fair? | `queue_no` from a sequence, taken under the event lock. Gaps (from rollbacks) don't matter, only the order does. |
| Is registration idempotent? | Its *effect* is: PUT never creates a second seat, and a retry returns the current registration. But it doesn't replay the exact original response, and a very late retry after cancelling would re-register. Exact replay needs an `Idempotency-Key`, which is on the roadmap. |
| Why PUT, not POST? | "Make sure I'm registered" is idempotent by meaning, and HTTP defines PUT as idempotent. |
| How do tickets work? | `registration id + version`, signed with HMAC-SHA256 using a server secret. Viewing doesn't change anything. Reissuing or cancelling increases the version, so old tickets die. |
| Why HMAC and not storing a hash? | A stored hash can't be shown again, so every view would have to rotate the token, invalidating screenshots, and JSON plus QR would rotate twice. Signed tickets can be re-shown safely, and invalidation is just `version + 1`. |
| Isn't HMAC like a JWT? | Same idea (a server-signed claim), but much smaller (about 75 characters, so a readable QR) and with one purpose. Separate secret from the JWT one. |
| Can someone share their QR? | Yes, it's a bearer credential like a cinema ticket. Check-in shows the name for an ID check, and a student can reissue to kill a copied ticket. |
| What's in a JWT? | header.payload.signature, **signed, not encrypted**. **Hook: a transparent envelope with a wax seal.** So no secrets go inside. |
| Which claims do you validate? | exp, iat, sub (a UUID), iss, aud, jti, all **required**. Algorithm pinned to HS256. |
| Why no role in the token? | It would stay valid for 30 minutes after a demotion. Loading the role from the DB makes the change immediate. |
| Logout? | Stateless tokens can't be revoked. Expiry is 30 minutes. The `jti` claim enables a future denylist. |
| Why not OAuth2 password flow? | RFC 9700 says the password grant MUST NOT be used. We're a first-party API, not an OAuth server: a JSON login plus bearer tokens. |
| Why Argon2id, and why 15 characters? | Argon2id is slow and memory-hard, so GPU guessing is expensive (OWASP's first choice). NIST SP 800-63B-4 requires at least 15 characters for single-factor passwords, a blocklist, and no forced symbols. |
| 401 vs 403? | 401: who are you? 403: I know you, and you're not allowed. |
| Why 404 for a draft? | 403 would confirm it exists. |
| Why UUIDs? | They can't be guessed or counted. Order comes from `queue_no` instead. |
| Rate limits, per what? | Explicit keys: IP for anonymous endpoints, account for logged-in ones, email for login. In-memory means single process. Redis is the roadmap fix. |
| Can someone hoard seats with fake accounts? | Partly. The limits and the domain allowlist help, and organisers can remove registrations. The real fix is verified college email, which is listed as a limitation. |
| How do you make sure tests can't wipe real data? | The guard checks that the parsed DB name is exactly `campuspass_test`, then confirms with `current_database()`, and fails closed. There's a test proving it refuses the real DB. |
| Why Alembic? | `create_all()` can't change existing tables safely. Migrations are versioned and reviewable, and the tests run them. |
| How do you know it works? | Run the functional, security, integrity and race-test suite against PostgreSQL and inspect final database state. Quote only observed output; added regression tests must pass before the updated ZIP is called verified. |
| At 1 lakh users? | Redis rate limits, more workers behind a load balancer, cached listings, a background queue for notifications, read replicas, possibly atomic counters for hot events. Measure first with the load script. |
| Did you use AI? | Answer honestly. Explain how you used it (for example as a pair programmer and reviewer), and show understanding by explaining each decision yourself. |

---

## 25. Definition of done (Phase 1)

- [ ] The event-first lock order applies to registration, cancellation, organiser removal, promotion, edits, publish/cancel/delete, ticket reissue and check-in. Where eligibility needs user rows, those locks follow registration locks and are acquired in UUID order. SHARE is never upgraded.
- [ ] Check-in cannot succeed after an event cancellation commits (test 5).
- [ ] Ticket view, reissue and invalidation behave as documented (tests 4, 6, 7).
- [ ] Registration and cancellation have per-account and per-IP limits, with the keys documented and tested (test 11).
- [ ] Row-level and email constraints exist under the exact names in §20 and are tested (test 10).
- [ ] DB errors are mapped precisely, with rollback (test 13). Lock and statement timeouts are active.
- [ ] Login is a JSON + Bearer flow (no OAuth password grant). JWT claims are required and validated.
- [ ] PostgreSQL 16+ (or 15, documented) and Python 3.12. Versions are recorded in the README.
- [ ] The test DB guard verifies the real database and is tested (test 12).
- [ ] The schema comes only from Alembic migrations. There's no `create_all`.
- [ ] Locked `requirements*.txt` files are committed, and the README installs from them.
- [ ] All tests in §14 pass against PostgreSQL, and the observed output is quoted in the README.
- [ ] The README separates implemented features, limitations and roadmap. The repo is public and checked in an incognito window. There are no secrets in git history.
- [ ] I can answer every question in §24 without notes.

---

## Appendix A: Tally of the "v2.2 Required Changes" review

Legend: ✅ adopted as written · 🔧 adopted, with a better or adjusted fix · ⚠️ the review itself needed a correction

| # | Review item | Verdict | Where in v3.0 | Notes |
|---|---|---|---|---|
| 1 | One lock order for all event-state operations | ✅🔧 | §8 | Adopted (event → registration everywhere). **Refined** with two lock strengths: FOR UPDATE for state changes, FOR SHARE for check-in and reissue, so door scanners work in parallel while cancellation still serializes against them. PostgreSQL's conflict table confirms SHARE∥SHARE is allowed and SHARE⟂UPDATE conflicts. Added the **never-upgrade rule** to prevent upgrade deadlocks. |
| 2 | Event cancellation vs check-in race | ✅ | §11, test 5 | The review was right: v2.1's check-in `UPDATE` never re-checked the event status under a lock. Check-in now takes the event lock first. The test asserts the invariant `checked_in_at ≤ cancelled_at`. |
| 3 | Transactional ticket issuance and rotation | 🔧 | §11, tests 6–7 | The problems were real (concurrent issuance; JSON and QR rotating twice). **Different fix:** versioned HMAC tickets instead of a stored hash with rotate-on-issue. Viewing no longer rotates, JSON and PNG are the same token by construction, reissue and cancel do `version + 1` (the equivalent of "clear the hash"), and nothing secret is stored. Concurrent reissues are serialized and documented exactly as the review asked. |
| 4 | Rate-limit registration and cancellation, with explicit keys | ✅🔧 | §6, test 11 | Adopted. The review correctly caught that v2.1 said "per user" for a limit that was actually keyed by IP. **Library change:** `slowapi` IP decorators → `limits` called from a FastAPI dependency, which can key by account *after* authentication. Also added: the conflict between rate limits and concurrency tests (test-only disable flag), `Retry-After`, the fail-open/closed policy, and an explicit note that **rate limits can't stop multi-account hoarding**. |
| 5 | Stronger row-state constraints | ✅🔧 | §3, §20, test 10 | Adopted the waitlist and confirmed checks. The review's third check required `checkin_token_hash` for `checked_in`, but that column no longer exists (item 3), so it's replaced by `checked_in_at` + `checked_in_by`. Added the cancel-fields check, the `cancelled_at` ↔ event status check, and an explicit **cycle timestamp table**. History moves to the Phase 2 audit log, as the review suggested. |
| 6 | `CHECK (email = lower(btrim(email)))` | ✅ | §3, §20 | Adopted as written, plus a named unique constraint mapped in §12 |
| 7 | Precise DB exception handling | ✅🔧 | §12, test 13 | Adopted: rollback first, map **by constraint name** (via the naming convention and psycopg's `diag.constraint_name`), unknown means 500. **Extended** to lock and statement timeouts (55P03, 57014 → 503) and deadlocks (40P01 → 503, logged as a bug). |
| 8 | Drop the OAuth2 password-grant form | ✅ | §5 | Verified: RFC 9700 §2.4 states the ROPC grant "MUST NOT be used". Now a JSON login + HTTP Bearer, explicitly *not* an OAuth server. `python-multipart` removed. |
| 9 | Supported PostgreSQL version | ⚠️ | §1 | Recommendation adopted (16/17/18). **Correction:** the review said that on 26 Oct 2026 only 16, 17 and 18 are supported. In fact **14 (until 12 Nov 2026) and 15 (until 11 Nov 2027) are also still supported**. 13 is unsupported. 14 is still a poor choice because it ends in about two weeks. |
| 10 | Stronger test-DB guard | ✅🔧 | §14, test 12 | Adopted all four conditions. Added a `current_database()` check after connecting and a host/port/db mismatch check. |
| 11 | `create_all()` is not migrations | ✅🔧 | §15, §21 | Went further: **Alembic from step 1**, `create_all()` removed entirely, tests run `alembic upgrade head`, and the manual-review checklist for autogenerate output (sequence, enums, CHECKs, partial index). |
| 12 | Real dependency locking | ✅🔧 | §17 | Adopted, with a concrete tool: `uv pip compile --universal`, because `pip freeze` on Windows isn't portable to Linux CI. `.in` + `.txt` files are committed, and reviewers install with plain pip. |
| 13 | Don't overclaim idempotency | ✅🔧 | §9, §24 | Wording fixed. **Improved:** registration is now `PUT`, so a retry returns `200` + the current registration instead of `409`. The exact limits (no byte-identical replay, and a late retry after cancelling re-registers) are stated, with `Idempotency-Key` in Phase 3. |
| 14 | Stronger JWT validation | ✅🔧 | §5 | `iss`, `aud`, `jti` added. `require` list used (PyJWT only checks `exp`/`iat` if present). `sub` validated as a UUID. 10-second leeway. |
| 15 | Redis, CI, deployment etc. stay roadmap until done | ✅ | §0 | Phases 2 and 3 |
| 16 | Load report only after correctness | ✅ | §14 | Phase 2, with the exact report fields |
| 17 | Keep it explainable | ✅ | §0, §2 | No repository layer, no queues. Every component has a stated reason. |
| T1–T13 | Required tests | ✅🔧 | §14 catalogue | All 13 are included (tests 6 and 7 adapted to versioned tickets). Added test 14 (last-admin race), a live-server + barrier harness, repeated runs, and DB-state assertions. |
| R | README updates | ✅ | §22 | Invariant, lock protocol, locks vs constraints, sequence gaps, idempotency wording, limiter limits, versions, observed results, limitations and roadmap |
| I | Interview updates | ✅ | §23, §24 | Includes the cancellation/check-in demo, the bearer-credential explanation and the ID check |
| C | Completion checklist | ✅ | §25 | Merged and extended |

---

## Appendix B: New findings from this review (not in the v2.2 document)

| # | Problem in v2.1 | Fix in v3.0 | Where |
|---|---|---|---|
| B1 | Viewing a ticket rotated it, so opening the app twice killed the screenshot sent to a friend or saved for the gate (poor usability) | Versioned HMAC tickets: viewing is repeatable | §11 |
| B2 | Check-in serialized on an exclusive lock would make all door scanners queue | FOR SHARE for check-in and reissue | §8 |
| B3 | SQL `now()` is the transaction **start** time, so deadline checks could pass after waiting for a lock | The clock is read after locks (`now_utc()` in app code) | §8 Rule 3 |
| B4 | No `lock_timeout` or `statement_timeout`: one stuck transaction could hang every request | 3s / 10s, with 503 BUSY + Retry-After | §8 Rule 4, §12 |
| B5 | v2.1 had dropped v1's request-size limit. Pydantic caps only apply after the full body is read. | Body-limit middleware (413/411) | §6 |
| B6 | Per-account limits with slowapi would need the user before the decorator runs | `limits` + a dependency after auth | §6 |
| B7 | Rate limits would break concurrency test 2 (50 requests from one account) | Test-only disable flag, plus separate rate-limit tests | §6, §14 |
| B8 | Without a version bump on cancel, a re-confirmed student's **old** ticket would come back to life | `ticket_version += 1` on cancel | §3, §11 |
| B9 | Organisers couldn't remove fake or duplicate registrations | Organiser removal endpoint with `cancelled_by` | §10 |
| B10 | Two admins disabling each other at once could leave zero admins | Last-admin guard with a row lock, plus the break-glass script | §5, test 14 |
| B11 | Promotion/check-in eligibility could race with account deactivation | Promotion locks waitlist users before checking activity; check-in locks attendee and scanner users and rechecks both eligibility and authorization. | §8, §10, §11 |
| B12 | The password minimum of 8 is below current NIST guidance for single-factor passwords | 15–128, a blocklist, no composition rules, NFC | §5 |
| B13 | A single shared secret would break both login and tickets if leaked | Separate `TICKET_SECRET`, validated as different | §11, §15 |
| B14 | `TestClient` in threads doesn't exercise the real server path | Live uvicorn test server + `threading.Barrier` | §14 |
| B15 | A retry after the registration window closed would wrongly get `REGISTRATION_CLOSED` | Check the existing row **before** the window | §9 step 4 |
| B16 | The event listing showed long-finished events by default, and its per-event counts risked N+1 queries | `include_past=false` default, one aggregate query | §7 |
| B17 | Constraint names were implicit, making error mapping fragile | MetaData naming convention | §2 |
| B18 | Ticket tokens in URLs would leak into logs and history | Check-in token in the POST body. Logs record route templates only. | §11, §12 |
| B19 | `cancelled_at` on events wasn't tied to status | `ck_events_cancelled_at` | §20 |

---

## Appendix C: Version history
- **v1** (your first spec): broad production checklist. Over-scoped, plus several gaps.
- **v2**: scope tiers, the lock-before-count design, FIFO `queue_no`, pinned JWT, Argon2id, 29 fixes.
- **v2.1**: Windows setup, dependencies, file tree, API reference, SQL, demo and interview prep.
- **v2.2 review** (your document): 17 change items + 13 tests. All resolved in Appendix A.
- **v3.0** (this document): the merged complete design, with versioned HMAC tickets, two-strength lock protocol, PUT registration, NIST password policy, Alembic from step 1, universal lock files, and 19 additional fixes (Appendix B).


### Implementation update notes for this ZIP revision

- Promotion locks all waitlisted registration rows in FIFO order, then locks the associated users in ascending UUID order. It checks `is_active` and event-owner eligibility only after those locks are acquired and the rows refreshed; it does not filter inactive users before locking. Admins may register for events they created, while organisers cannot register for their own events.
- Check-in locks the event, target registration, then attendee and scanner user rows in UUID order. It re-checks scanner authorization and attendee activity, then reads the clock and verifies the check-in window.
- Admin user changes lock the actor, target, and currently active admins in the same UUID order and re-check the actor after acquiring locks.
- The cancellation-actor constraint is strengthened by Alembic revision `0002_require_cancelled_by.py`. The existing `0001_initial_schema.py` is preserved as historical migration history.
- The updated ZIP includes new regression tests, but those tests must be run against PostgreSQL before this revision is described as verified. Dependency lock files must be generated on a network-connected machine; the current archive notes this limitation explicitly.

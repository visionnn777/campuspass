# CampusPass Learning Guide

This guide teaches the whole project from scratch, in the same order it was built (spec §21, steps 1–9). It assumes only a Class 12 background. Each step has a goal, the concepts with a memory hook, what to read in the code, how to run and verify it, interview questions and a self-check.

**How to use it:** do the steps **in order**. For each step, read the files listed, run the commands, then answer the self-check *before* opening the answers. Commit after each step (see the [commit plan](#commit-plan-make-the-history-your-own) at the end). That way your Git history shows the project growing, and you understand every file you commit.

---

## The big picture in one minute

```
HTTP request
  -> RequestContextMiddleware   (gives it a request ID, logs it)
  -> BodySizeLimitMiddleware    (rejects huge bodies early)
  -> router (app/routers)       (reads input, calls ONE service function)
       -> deps: get_current_user (who is calling? loaded from the DB)
       -> deps: rate limit       (too many requests?)
  -> service (app/services)     (the business rules, inside ONE transaction, with locks)
  -> models (app/models)        (tables + constraints in PostgreSQL)
  <- schema (app/schemas)       (shapes the JSON response; never leaks secrets)
  <- errors (app/core/errors)   (any failure becomes the same JSON error shape)
```

**Hook: a restaurant.** The middleware is the doorman, the router is the waiter (takes the order, doesn't cook), the service is the chef (all the real work), the database is the pantry with locks, and the schema is the plate (only what the guest should see).

---

## Step 1: Skeleton, config, schema, DB guard

**🎯 Goal:** configuration that refuses unsafe values, a database connection with timeouts, three tables with strong constraints created by a migration, health checks, and a guard so the tests can never wipe your real data.

### 🧠 Concepts
- **Environment variables and `.env`.** Secrets (database password, JWT secret) must never be written in code or committed to Git. They live in `.env`, which is git-ignored. `config.py` reads them into a typed `Settings` object and **refuses to start** if a secret is weak or a placeholder.
  *Hook:* the `.env` file is your house key. You don't paste it on the front door (GitHub).
- **ORM (SQLAlchemy).** Python classes describe tables (`class User` → table `users`). You write `select(User).where(...)` instead of raw SQL strings.
- **Constraint.** A rule the *database* enforces, whatever the code does. `UNIQUE(event_id, user_id)` means a student can have at most one row per event, so even buggy code can't create a second.
  *Hook:* constraints guard each seat card; the lock (step 6) guards the hall door.
- **Migration (Alembic).** A versioned script that changes the schema (`0001_initial_schema.py`). `alembic upgrade head` applies it, and `alembic downgrade base` undoes it. **Why not `create_all()`?** It can create tables but can't safely change existing ones later.
- **Naming convention.** Every constraint gets a predictable name (`uq_registrations_event_id`). Step 2 uses these names to turn database errors into precise API errors.
- **Timezones.** Every timestamp is `timestamptz` in UTC. The API rejects times without an offset, because "10:00" alone could mean IST or UTC.

### 📁 Read these files
| File | Look for |
|---|---|
| `app/core/config.py` | `_secret_must_be_strong`, the check that `JWT_SECRET != TICKET_SECRET`, and that rate limits can be off only in tests |
| `app/core/database.py` | `connect_args` → `lock_timeout` / `statement_timeout` (step 6 explains why) |
| `app/core/clock.py` | Why business time is `now_utc()` and not SQL `now()` |
| `app/models/registration.py` | Each `CheckConstraint`, and read them out loud in plain English |
| `alembic/versions/0001_initial_schema.py` and `0002_require_cancelled_by.py` | Review the initial schema and the follow-up constraint migration; historical migrations are not edited in place |
| `tests/db_guard.py`, `tests/conftest.py` (top) | The four guard conditions, run **before** the app is imported |

### ▶️ Run it
```powershell
alembic upgrade head
psql -U campuspass -d campuspass -c "\d registrations"   # see every constraint by name
alembic downgrade base; alembic upgrade head              # verify the full migration round trip
pytest tests/test_db_guard.py -v                         # expected: 6 passed
```

### 🎤 Interview questions
- *Why Alembic and not `create_all()`?* Migrations are versioned and reviewable, and can change existing tables. `create_all` only creates what's missing.
- *What must you inspect in a generated migration?* Autogenerate may miss the standalone sequence, enum types, CHECK constraints, and partial index. Review them by hand and run the migration round trip on PostgreSQL before claiming it passed.
- *How do you make sure tests can't delete real data?* The parsed database name must be exactly `campuspass_test`, it must differ from `DATABASE_URL`, and after connecting `SELECT current_database()` must confirm it. If anything can't be verified, the run stops.

<details><summary>🧪 Self-check</summary>

1. *A row has `status = 'confirmed'` and `queue_no = 7`. What stops it?* `ck_registrations_waitlist_fields`: only waitlisted rows may have a queue number.
2. *Why does the app refuse to start when `JWT_SECRET == TICKET_SECRET`?* Key separation: one leaked key shouldn't let an attacker forge both login tokens and tickets.
</details>

---

## Step 2: Errors and middleware

**🎯 Goal:** every error looks the same, every response has a request ID, huge bodies are rejected before they're read, and database errors are mapped **precisely**.

### 🧠 Concepts
- **One error shape:** `{"error": {"code", "message", "request_id"}}`. Clients can switch on `code`, and you can find the exact log line with `request_id`.
- **Middleware:** code that wraps every request, like airport security that every passenger passes through.
- **Why a body-size limit in middleware?** Pydantic checks lengths only *after* reading the whole body. A 500 MB request would already be in memory by then.
- **SQLSTATE:** PostgreSQL's error code. `23505` = unique violation, `55P03` = lock timeout, `40P01` = deadlock. `db_errors.py` maps **by SQLSTATE + constraint name**, never "any IntegrityError → already registered".
  *Hook:* a doctor diagnoses from the specific symptom, not "you feel unwell, so it's flu".
- **Never echo input in errors.** If validation fails on a password field, the response must not contain the password.

### 📁 Read
`app/core/errors.py` (the 5 handlers), `app/core/db_errors.py` (the mapping table), `app/core/middleware.py` (request ID, body limit, JSON log).

### ▶️ Run
```powershell
pytest tests/test_middleware.py tests/test_db_errors.py -v
```

### 🎤 Interview questions
- *What happens if the database is down?* `503 DATABASE_UNAVAILABLE`. An outage is never shown as a business error like "already registered".
- *What if an unknown constraint is violated?* `500 INTERNAL_ERROR`, logged as a bug. Only known, named constraints become 4xx errors.

<details><summary>🧪 Self-check</summary>

1. *A lock wait times out. Which status code, and which header?* 503 `BUSY` with `Retry-After: 1`.
2. *Why reuse an incoming `X-Request-ID` only if it's a valid UUID?* To stop log injection (for example a value containing newlines).
</details>

---

## Step 3: Auth, roles, admin

**🎯 Goal:** students sign up, everyone logs in with JSON, gets a JWT, and each request is checked against the database.

### 🧠 Concepts
- **Hashing vs encryption.** A hash is one-way: you can't get the password back. Login hashes the attempt and compares it with the stored hash.
- **Argon2id:** a deliberately *slow, memory-hungry* hash, so guessing billions of passwords on a GPU is expensive. A **salt** (random bytes per user) makes two people's identical passwords hash differently.
  *Hook:* Argon2 is a heavy lock; SHA-256 is a light latch. For passwords you want the heavy one.
- **NIST password policy (2025):** at least 15 characters, up to 128, no "must contain a symbol" rules, and a check against a list of common passwords.
  *Hook:* long and unusual beats short and complicated: "nehru place chai at 4 pm" beats "P@ssw0rd1".
- **JWT:** `header.payload.signature`. It's **signed, not encrypted**, so anyone can *read* the payload, but nobody can *change* it without the secret.
  *Hook:* a transparent envelope with a wax seal. Never put secrets inside.
- **Why no role inside the token?** A token lives for 30 minutes. If an admin demotes someone, an old token would still say "organiser". CampusPass loads the user from the DB on every request instead.
- **Pinned algorithm:** `algorithms=["HS256"]` is hard-coded. If the server trusted the token's own header, an attacker could send `alg: none` (no signature at all).
- **Timing-safe login:** for an unknown email the server still runs a password check against a dummy hash, so "no such user" takes as long as "wrong password".
- **401 vs 403:** 401 means "who are you?", 403 means "I know you, and you're not allowed".
- **Not the OAuth2 password grant:** RFC 9700 says that grant must not be used. CampusPass is a first-party API with a plain JSON login and Bearer tokens.

### 📁 Read
`app/core/security.py`, `app/core/deps.py`, `app/schemas/auth.py` (`extra="forbid"`), `app/services/auth_service.py`, `app/services/admin_service.py` (the last-admin guard), `scripts/create_admin.py`.

### ▶️ Run
```powershell
pytest tests/test_auth.py tests/test_admin.py -v
python -m scripts.create_admin
```
In Swagger: `POST /auth/login` → copy the token → **Authorize** → `GET /auth/me`.

### 🎤 Interview questions
- *Signup with `"role": "admin"`?* 422. `extra="forbid"` rejects unknown fields (mass-assignment protection).
- *Which JWT claims do you validate?* `exp, iat, sub, iss, aud, jti`, all required, with HS256 hard-coded.
- *How do you log out?* A stateless JWT can't be revoked, so it expires in 30 minutes. The `jti` claim allows a denylist later.

<details><summary>🧪 Self-check</summary>

1. *Why does the API return the same 401 for "unknown email" and "wrong password"?* So attackers can't find out which emails are registered.
2. *Two admins disable each other at the same moment. What stops zero admins remaining?* Each update locks all active admin rows (in id order) and then counts. The second one sees only one admin left and gets `409 LAST_ADMIN`.
</details>

---

## Step 4: Rate limiting

**🎯 Goal:** limit abuse with explicit keys.

### 🧠 Concepts
- **Rate limit:** "at most N requests per time window per key". The **key** decides who is counted: the IP for anonymous endpoints, the account for logged-in ones, the email for login.
- **Moving window:** counts the requests in the *last* 60 seconds, not "this calendar minute", so it can't be gamed at minute boundaries.
- **`X-Forwarded-For` can be faked:** anyone can send that header, so it's trusted only behind a known proxy.
- **The limitation:** counters live in memory, so they reset on restart and aren't shared between processes. Run one worker. Redis is the roadmap fix.

### 📁 Read
`app/core/ratelimit.py`, then find `ip_limit(` / `user_limit(` in `app/routers/*.py`.

### ▶️ Run
`pytest tests/test_rate_limits.py -v`

### 🎤 Interview question
- *Can rate limits stop one person making 20 accounts to grab 20 seats?* Only partly. That needs verified college email (roadmap). Organisers can remove fake registrations.

<details><summary>🧪 Self-check</summary>

1. *Why is the per-account registration limit applied after authentication?* The key is the user id, which is known only after the token is checked.
</details>

---

## Step 5: Events

**🎯 Goal:** organisers create drafts, publish, edit, cancel and delete drafts, and anyone can browse.

### 🧠 Concepts
- **Draft → published → cancelled.** "Completed" is **computed** from `ends_at`, never stored, so it can't go stale.
- **404 for someone else's draft:** a 403 would confirm that it exists.
- **Pagination:** `page` and `page_size` (max 100). Without the cap, `?page_size=1000000` loads the whole table.
- **LIKE escaping:** in SQL, `%` and `_` are wildcards, so user input is escaped and a search for "100%" means the literal text.
- **N+1 problem:** fetching 20 events and then running 2 count queries *per event* means 41 queries. The listing uses one statement with correlated subqueries instead.

### 📁 Read
`app/services/event_service.py`: `require_manage_access`, `_list_statement`, `update_event` (capacity rules plus promotion).

### ▶️ Run
`pytest tests/test_events.py tests/test_permissions.py -v`

<details><summary>🧪 Self-check</summary>

1. *Why can't a published event be deleted?* It may have registrations (history). Cancelling keeps them, and foreign keys use RESTRICT.
</details>

---

## Step 6: The lock protocol, registration and waitlist ★ the heart of the project

**🎯 Goal:** no overbooking, ever. A FIFO waitlist, and promotion in the same transaction as a cancellation.

### 🧠 Concepts, slowly
1. **Transaction:** a group of database changes that happen *all together or not at all*. If the server crashes halfway, PostgreSQL undoes everything.
   *Hook:* an online payment. Either the money leaves your account **and** arrives, or neither happens.
2. **Race condition:** two requests read "1 seat left" *before* either one writes. Both book it, and the event is overbooked.
   *Hook:* two hands grabbing the last samosa.
3. **Row lock (`SELECT ... FOR UPDATE`):** "I'm working on this row. Anyone else who wants to lock it must wait until I commit."
4. **Why count *after* the lock?** In PostgreSQL's default mode (READ COMMITTED), each statement sees data committed before *that statement* began. A count that runs after you got the lock therefore includes the previous person's booking.
   *Hook:* **lock the door, then count the chairs.**
5. **Why read the clock after the lock?** SQL `now()` is when the *transaction* started. If you started at 09:59:59 and waited for the lock until 10:00:01, `now()` would still say 09:59:59 and you'd sneak past a 10:00 deadline.
6. **Lock order:** every transaction locks the **event first, then registrations**. If two transactions each held what the other needed, they'd wait forever (a *deadlock*). The same order everywhere makes that impossible.
7. **`FOR UPDATE` vs `FOR SHARE`:** UPDATE allows one holder at a time; SHARE allows many holders, but no writer.
   *Hook:* the notice board. Many can read it together; to rewrite it, everyone must step away.
8. **Sequence for FIFO:** `queue_no` comes from `waitlist_queue_seq`, taken under the lock. Gaps (from rolled-back attempts) don't matter; only the order does.
9. **PUT = "make sure I'm registered":** repeating it never creates a second seat. The first call gets 201, the rest get 200.

### 📁 Read slowly, line by line
**`app/core/locks.py`:** `lock_event_for_update` (note `populate_existing`), `count_occupancy`.

**`app/services/registration_service.py → register()`**, the journey of one request:
```
with db.begin():                         # 1. start ONE transaction
  event = lock_event_for_update(...)     # 2. lock the event row (others wait here)
  t = now_utc()                          # 3. read the clock AFTER the lock
  cancelled? organiser? -> errors
  registration = lock_registration_of_user(...)
  already active? -> return it (200)     # 4. checked BEFORE the window, so retries get an answer
  outside window? -> 409
  occupancy, waiting = count_occupancy() # 5. count AFTER the lock
  seat free -> confirmed | waitlist room -> waitlisted (nextval) | else 409 EVENT_FULL
  insert or reuse the row; flush         # 6. UNIQUE constraint as a safety net
# 7. leaving the with-block COMMITS, which releases the lock; the next request goes
```
**`_cancel_locked()`:** cancelling a confirmed seat bumps `ticket_version` (old tickets die), then `promote(db, event, 1, t)`.
**`app/services/promotion.py`:** the one FIFO query. `ORDER BY queue_no LIMIT n FOR UPDATE OF registrations`, skipping inactive users.

### ▶️ Run
```powershell
pytest tests/test_registrations.py -v
pytest tests/test_concurrency.py -k "test_1_ or test_2_ or test_3_ or test_8_" -v
```
**Try the proof yourself:** in `lock_event_for_update`, temporarily delete the `.with_for_update()` line and run `pytest tests/test_concurrency.py -k test_1_`. You'll see more than 10 confirmed for 10 seats (when we did this: 23, 18 and 15). Put the line back. Being able to say *"I removed the lock and watched the test catch the overbooking"* is a strong interview moment.

### 🎤 Interview questions
- *Why not SERIALIZABLE isolation?* It's also correct, but conflicting transactions *fail* and must be retried. Locks make them *wait*, which is simpler and predictable.
- *Doesn't a lock slow everything down?* Only requests for the *same* event wait, and each holds the lock for milliseconds. Other events aren't affected. At very large scale you'd consider an atomic counter.
- *Two people cancel at the same time. Can the same waiter be promoted twice?* No. Both need the event's exclusive lock, so they run one after the other, and the second promotes the *next* person.
- *What do constraints add if you already have locks?* Locks protect totals; constraints protect each row. That's defence in depth.

<details><summary>🧪 Self-check</summary>

1. *What exactly goes wrong if `count_occupancy` runs before `lock_event_for_update`?* Two requests can both count "1 free" from the same snapshot, and both confirm.
2. *A student cancels and re-registers while the event is full. Where do they go?* To the **back** of the waitlist, with a new, larger `queue_no`.
</details>

---

## Step 7: Tickets and check-in

**🎯 Goal:** a ticket that can be shown many times but used only once, and never after the event is cancelled.

### 🧠 Concepts
- **HMAC:** a signature made with a secret key. Anyone can read the ticket (`registration id + version`), but only the server can produce a valid signature.
  *Hook:* a teacher's signature on a hall pass. Students can read it, but can't forge it.
- **Versioned tickets:** the ticket includes `ticket_version`. Reissue or cancellation does `version + 1`, so every older ticket stops matching. Viewing changes nothing, so a screenshot stays valid.
- **Constant-time comparison (`hmac.compare_digest`):** a normal `==` stops at the first different character, and the timing could leak how much of a guess was right.
- **Bearer credential:** whoever holds the QR gets in, like a cinema ticket. That's why check-in shows the **name**, so the volunteer can compare it with the college ID.
- **`FOR SHARE` at the door:** many scanners work in parallel. Cancelling the event (`FOR UPDATE`) waits for them, and any scan after the cancellation reads "cancelled" and fails.

### 📁 Read
`app/core/tickets.py` (`build_ticket`, `parse_ticket`), `app/services/ticket_service.py` (`check_in`, steps 0–7 in the comments).

### ▶️ Run
```powershell
pytest tests/test_tickets.py -v
pytest tests/test_concurrency.py -k "test_4_ or test_5_ or test_6_ or test_7_" -v
```

### 🎤 Interview questions
- *Why not store a hash of the ticket?* A hash can't be shown again, so every view would have to replace the ticket, killing screenshots, and the JSON and QR views would rotate twice. Signed tickets can be re-shown, and invalidation is just `version + 1`.
- *Can a ticket for event B be used at event A?* No. `event_id` is part of the lookup, so it's "not found".
- *What if the event is cancelled while someone is being scanned?* Whichever gets the event lock first wins. If check-in wins, the student was really admitted before the cancellation. If cancellation wins, the scan fails with `EVENT_NOT_ACTIVE`. In our tests both orders happened (13 vs 17 out of 30), and the invariant always held.

<details><summary>🧪 Self-check</summary>

1. *A student cancels, then re-registers and is confirmed again. Does their old screenshot work?* No. Cancellation bumped the version.
2. *Why is the ticket sent in the POST body and never in the URL?* URLs end up in logs, browser history and proxies.
</details>

---

## Step 8: The integrity and race suite

**🎯 Goal:** prove the guarantees with real concurrency, and check the **final database state**, not just status codes.

### 🧠 Concepts
- **Live server + barrier:** the real app runs in a background thread, and 50 threads with their *own* HTTP clients wait at a `threading.Barrier` until all are ready, then fire at once.
  *Hook:* runners in their lanes, all waiting for the starting gun.
- **Assert on the database:** "50 responses were 201" isn't enough. The test counts confirmed rows in PostgreSQL: exactly 10.
- **Repeat the runs:** race bugs are probabilistic, so each test runs several rounds (`CONCURRENCY_ROUNDS`).

### ▶️ Run
`pytest -m concurrency -v` and then the full `pytest -v`. Record the observed summary line; do not assume a historical test count applies to this updated revision.

---

## Step 9: README and release
Read `README.md` top to bottom, re-run the tests on **your** machine, and replace the "Test results" block with **your** observed output. Check the repo in an incognito window. Make sure `.env` is not in Git (`git ls-files | Select-String .env` should show only `.env.example`).

---

## Commit plan: make the history your own

Initialise your own repository and add the files **in the order of this guide**, reading each step before committing it. Your history then tells the build story, and you'll have actually read every file.

```powershell
git init
git add .gitignore .env.example requirements.in requirements-dev.in pytest.ini alembic.ini alembic app/__init__.py app/core/__init__.py app/core/config.py app/core/database.py app/core/naming.py app/core/clock.py app/models app/data app/routers/__init__.py app/routers/health.py tests/__init__.py tests/db_guard.py tests/test_db_guard.py
git commit -m "chore: skeleton, config, schema migration, db guard"
git add app/core/errors.py app/core/db_errors.py app/core/middleware.py app/schemas/__init__.py app/schemas/common.py tests/test_middleware.py tests/test_db_errors.py
git commit -m "feat: error handling and middleware"
git add app/core/security.py app/core/deps.py app/schemas/auth.py app/schemas/admin.py app/services/__init__.py app/services/auth_service.py app/services/admin_service.py app/routers/auth.py app/routers/admin.py scripts/__init__.py scripts/create_admin.py tests/test_auth.py tests/test_admin.py
git commit -m "feat: auth, roles, admin"
git add app/core/ratelimit.py tests/test_rate_limits.py
git commit -m "feat: keyed rate limiting"
git add app/schemas/event.py app/services/event_service.py app/routers/events.py app/routers/me.py tests/test_events.py tests/test_permissions.py
git commit -m "feat: events"
git add app/core/locks.py app/services/promotion.py app/services/registration_service.py app/schemas/registration.py app/routers/registrations.py scripts/seed_demo.py tests/conftest.py tests/live_server.py tests/test_registrations.py
git commit -m "feat: lock protocol, registration, FIFO waitlist"
git add app/core/tickets.py app/services/ticket_service.py app/routers/tickets.py app/routers/checkins.py app/main.py tests/test_tickets.py tests/test_concurrency.py tests/test_constraints.py
git commit -m "feat: versioned tickets, check-in and the race suite"
git add README.md docs licenses scripts/export_openapi.py tests/test_openapi_contract.py
git add requirements.txt requirements-dev.txt  # only after generated from uv pip compile
git commit -m "docs: README, OpenAPI, decisions and notices"
```
(Because the files already exist, the tests are only meaningful once all steps are added. Run the full suite before the final commit.)

If the panel asks how you built it, answer honestly: say what you used AI for (for example, as a pair programmer and reviewer), and show your understanding by explaining any file they point at.

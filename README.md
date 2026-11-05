# FiCol

FiCol is a Django field-collection platform for collectors and Back Office staff. It supports nearby account offers, exclusive account ownership, collection visits, follow-up scheduling, private evidence, payment reporting, and account release review.

This is intended for operational use. The current repository is still under development and is not a production deployment configuration.

Documentation baseline: **18 October 2026**. Status below distinguishes existing code, applied database migrations, historical test reports, owner-reported manual checks, and planned work. Applied migration state reflects the local PostgreSQL database (all 43 migrations applied: 25 application, 18 framework).

## Current status

| Area | Current state |
| --- | --- |
| Shared login and exclusive portal routing | Implemented; direct portal routing via user permissions/role; `/login/success/` was removed. Back Office authority takes precedence over a retained collector profile. |
| Collector dashboard, sidebar, navbar | Implemented with responsive navigation. Collector dashboard displays live assigned accounts with working account links; performance and reward summaries remain placeholders. |
| GPS submission and online/offline availability | Implemented with server validation and confirmed saving. |
| Map-based nearby offers | Implemented with Leaflet and OpenStreetMap tiles; normal matching checks still apply to test accounts. |
| Offer acceptance and My Accounts | Implemented with exclusive ownership and rolling acceptance limits. |
| Collection visits and history | Implemented; the owner reported successful Start Visit / Submit Visit testing. |
| Back Office visit review | Implemented; report review is separate from ownership, scheduling, and financial decisions. |
| Follow-up requests and staff scheduling | Implemented with earliest-start and completion-deadline windows. |
| Private evidence and payment reporting/verification | Implemented; reporting and verification do not themselves move money or update balances. |
| Collector release requests and Back Office decisions | Implemented; owner manually tested rejection, resubmission, approval, removal from active accounts, and preservation of acceptance count. Supports `superseded` status. |
| Manual Back Office assignment/reassignment | Implemented in code with applied migration (`assignments: 0005`) and registered routes. Supports assignment `event_id`, `manage_account_assignments` permission, and `superseded` release status. Concurrency tests and live operational verification remain pending. |
| Automatic inactivity/missed-follow-up reassignment | Not implemented; timing and exception policies need confirmation. |
| Structured account-information proposals and validation | Implemented in code with applied migration (`accounts: 0006`) across 5 categories with dedicated Back Office validation queue. Feature branch recorded locally as `feature/pito-information-validation`; PR/merge to `origin/main` remains pending based on local refs. |
| Points, commission awards, deductions, payouts | Not a completed workflow. Earning-rule models/snapshots are foundations, not a working payout engine. |
| WIRED integration | Deferred. |
| Embedded turn-by-turn navigation and offline synchronization | Not implemented. Directions currently open Google Maps externally. |
| PWA foundation and offline fallback | Implemented; installable shell for Chrome/Android via HTTPS ngrok, root manifest, service worker with network-first static asset caching, generic offline fallback, and update notice. Offline account data, offline field submissions, and background synchronization remain unimplemented. |

## Project layout

```text
FiCol/
├── apps/
│   ├── user/          # Login roles, collector profiles, client access
│   ├── accounts/      # Clients, customers, addresses, collection accounts
│   ├── assignments/   # Location, offers, ownership, release requests
│   ├── activities/    # Visits, follow-ups, document metadata
│   ├── collection/    # Earning terms, promises, payment reporting
│   ├── audit/         # Bounded audit evidence
│   ├── core/          # Shared models such as notifications
│   └── integrations/  # Reserved integration app
├── config/            # Django settings and root URLs
├── frontend/
│   ├── package.json
│   ├── package-lock.json
│   ├── tailwind.config.js
│   └── node_modules/  # Generated locally; ignored by Git
├── static/
│   ├── css/           # Shared, component, collector, Back Office CSS
│   │   ├── tailwind.input.css
│   │   └── dist/tailwind.css
│   ├── js/            # Separate scripts by page/component
│   └── img/favicon.svg
├── templates/         # Django pages and shared components
├── docs/              # Local architecture and implementation notes
├── private_media/     # Private uploads; never expose as public static assets
├── ficolenv/          # Local Python virtual environment
├── .env               # Local database configuration; ignored by Git
├── requirements.txt
└── manage.py
```

`frontend/` contains CSS build tooling, not a separate frontend application. Django renders the templates. Keep the frontend and backend coordinated in this repository.

## Local setup (Windows / PowerShell)

### Prerequisites

- Python: the existing development environment uses Python 3.13.7.
- Python dependencies: see `requirements.txt` (Django, python-dotenv, Psycopg).
- PostgreSQL with PostGIS enabled in the application database. The local development database environment was verified on PostgreSQL 18.6 with PostGIS 3.6.2 (historical records previously noted PostgreSQL 17 and PostGIS 3.5.3).
- GDAL and GEOS native libraries accessible to GeoDjango. Installing the database extension alone does not establish Python DLL discovery.
- Node.js and npm for the Tailwind build. Tailwind is pinned to 3.4.17 in `frontend/package.json` and the lockfile records dependency versions.

### 1. Install Python packages

For a fresh checkout, create and activate a local environment:

```powershell
python -m venv ficolenv
.\ficolenv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If the environment already exists, activate it rather than recreating it.

### 2. Configure the database

Create your own project-root `.env` using local values:

```dotenv
DB_NAME=ficol_db
DB_USER=your_database_user
DB_PASSWORD=your_local_password
DB_HOST=127.0.0.1
DB_PORT=5432
```

Do not commit credentials. Create the database and enable PostGIS using an authorized database administrator; do not change database privileges merely to bypass a setup error.

On Windows, `config/settings.py` defaults GIS library discovery to `C:\Program Files\PostgreSQL\17\bin`, with `libgdal-35.dll` and `libgeos_c.dll`. The local environment `.env` overrides this via `GIS_LIBRARY_DIR=C:/Program Files/PostgreSQL/18/bin`. If your installation differs, configure these environment variables to match actual installed paths:

- `GIS_LIBRARY_DIR`
- `GDAL_LIBRARY_PATH`
- `GEOS_LIBRARY_PATH`

### 3. Verify setup and inspect migrations

```powershell
python manage.py check
python manage.py showmigrations
python manage.py migrate --plan
```

On a new local development database, apply the reviewed migrations:

```powershell
python manage.py migrate
```

For an existing/shared database, coordinate migration application first. Preserve applied migrations and historical data. Resolve any historical duplicate ongoing visits explicitly before applying the single-ongoing-visit constraint.

Verify that Django can query PostGIS:

```powershell
python manage.py shell -c "from django.db import connection; c = connection.cursor(); c.execute('SELECT current_database(), PostGIS_Version()'); print(c.fetchone()); c.close()"
```

### 4. Install and build frontend dependencies

From the project root:

```powershell
npm --prefix frontend ci
npm --prefix frontend run build:css
```

For development watching:

```powershell
npm --prefix frontend run watch:css
```

The build scans Django templates and JavaScript. Rebuild after introducing utility classes. Use complete class names or an explicit safelist for dynamic classes. Do not hand-edit compiled `static/css/dist/tailwind.css`.

`npm ci` generates `frontend/node_modules/`; the build generates/updates `static/css/dist/tailwind.css`. Keep package manifests, the lockfile, configuration, and compiled stylesheet in version control. Do not commit installed dependency folders.

### 5. Start the local server

```powershell
python manage.py runserver
```

Open `http://127.0.0.1:8000/login/`. The development server is not the production application server.

## Accounts, roles, and permissions

Login identities share the custom `user.User` table. Clients/customers/collection accounts are business records, not login accounts. Both portals use the same account data with role, client-scope, and ownership checks; separate portal tables do not provide that authorization.

An ordinary collector needs an active user, an approved `CollectorProfile`, and active `ClientAccess` for an active client. Existing profile capacity values are retained but do not enforce the old five-account cap.

Back Office classification currently includes active superusers or users with any of these six permissions:

| Feature | Permission |
| --- | --- |
| Information validation | `accounts.validate_account_information` |
| Visit review | `activities.change_fieldactivity` |
| Follow-up scheduling | `activities.change_followuptask` |
| Payment verification | `collection.change_payment` |
| Release review | `assignments.review_release_requests` |
| Assignment management | `assignments.manage_account_assignments` |

`is_staff` alone is not Back Office workflow authority. A Back Office identity cannot use collector workflows even if it retains a historical collector profile. Superusers still need active client grants for scoped workflow access. Review actions prohibit self-review.

Create users through Django tooling/admin so passwords are hashed. Do not store plaintext passwords or commit development login credentials. Permission grants and client access must be configured intentionally; setup does not grant them automatically.

## Existing pages

| Page | URL |
| --- | --- |
| Login | `/login/` |
| Collector dashboard | `/collector/dashboard/` |
| Account Offers | `/collector/offers/` |
| My Accounts | `/collector/accounts/` |
| Assigned account detail | `/collector/accounts/<assignment_id>/` |
| Propose account information | `/collector/accounts/<assignment_id>/information/propose/` |
| Collector follow-ups | `/collector/follow-ups/` |
| Back Office dashboard | `/back-office/` |
| Visit reviews | `/back-office/visits/` |
| Follow-up scheduling | `/back-office/follow-ups/` |
| Payment verification | `/back-office/payments/` |
| Release requests | `/back-office/release-requests/` |
| Account assignments | `/back-office/assignments/` |
| Account assignment detail / assign | `/back-office/assignments/<account_id>/` |
| Information proposals queue | `/back-office/information-proposals/` |

Account-detail URLs use an assignment identifier, not an account identifier. Actions such as logout, acceptance, and review use protected POST requests.

## Operating rules and workflows

### Offers and ownership

1. The approved collector goes online and saves a valid location.
2. **Refresh offers** runs matching. Merely opening the page reads existing offers.
3. Matching requires an eligible account: allowed source, active client access, open status, verified address/coordinates, valid active earning terms, no current owner, and client/collector matching-policy compliance.
4. Before acceptance the map exposes an approximate area; acceptance grants exclusive ownership and reveals the assigned collection location.
5. Going offline pauses new offers; it does not release owned accounts.

Collectors may own multiple accounts but may have only **one ongoing visit** across accounts. The approved acceptance rule is **10 new offer acceptances per rolling 24 hours**, across clients and assignment statuses. Release/completion does not refund a slot. Manual assignments without an offer do not count as self-service offer acceptances; manual assignment is implemented in code and migrated locally (`assignments: 0005`), while operational and concurrency testing remains pending.

Only `TEST` source accounts are enabled by default through `COLLECTION_OFFER_SOURCE_SYSTEMS`. Fictional examples are not production customer data or approved real-world commission terms.

### Location and maps

Default submission limits are accuracy at most **50 m**, age at most **300 seconds**, and future clock skew at most **120 seconds**. Submission settings are provisional guardrails; matching additionally evaluates each client's policy. A stored location does not automatically qualify for every client.

The matching radius comes from the collector profile; the test profile used 5,000 m. Device-reported accuracy and distance from a customer pin are different measurements. Location is supporting evidence, not proof of customer contact.

Maps currently use Leaflet and OpenStreetMap tiles. External directions open Google Maps; there is no embedded turn-by-turn routing. Tile URLs and attribution are configurable through `MAP_TILE_URL` and `MAP_ATTRIBUTION`. Production map-provider usage and capacity must be reviewed before launch.

### Visits, review, and follow-ups

- Start a visit on arrival, submit its actual outcome, and retain the history. Existing code supports a location-issue explanation where location is unavailable/unsuitable; strict arrival proof is not a completed policy.
- New submitted reports enter Back Office review. Review does not release ownership, authorize a revisit, update balances, or award compensation/points.
- After submission, further visits require the appropriate follow-up workflow. Collectors request a revisit; authorized staff schedules or rejects it.
- Authorized schedules have an earliest start and completion deadline. New starts outside the permitted window are blocked; a visit already in progress can finish late with its actual completion time recorded.
- Historical schedules and reports may follow compatibility paths; do not rewrite their status automatically.

### Payment reports and private evidence

Collectors report customer payments to authorized channels and upload evidence; FiCol does not receive customer funds. Back Office independently verifies or rejects the report. Reporting/verification alone does not change balances, close accounts, fulfill promises, or create commissions/points.

Documents are stored under `private_media/` and streamed through authorized views. Current collector access requires approved active ownership and client scope, including for the original uploader. Do not publicly serve that directory. Evidence validation accepts configured JPEG, PNG, WebP, and PDF uploads with content/size checks; the current maximum is 5 MB.

### Release requests

1. The collector submits a reason while retaining active ownership.
2. Release requests must be enabled by the client's `allow_collector_release` policy.
3. Authorized client-scoped Back Office staff rejects with a reason, or approves when no visit is in progress.
4. Rejection preserves ownership and work. Approval ends the assignment, clears its next-action deadline, and cancels its outstanding requested/pending follow-ups while preserving history.
5. The former owner loses account/actions/private-evidence access. Rolling acceptance usage remains unchanged.

Normal matching may subsequently offer an eligible released account. Release approval does not itself select a replacement collector. Request statuses are `requested`, `approved`, `rejected`, and `superseded` (applied in `assignments: 0005`, where administrative reassignment supersedes pending release requests with a required reason).

## Testing and verification

Basic environment checks:

```powershell
python -m pip check
python manage.py check
node --check static/js/dashboard/collector.js
node --check static/js/collector/account_offers.js
```

In the current environment, `python manage.py check` passed with zero issues, and `showmigrations` confirmed that all 43 migrations (25 application migrations across 7 custom apps, 18 Django framework migrations) are applied in the target PostgreSQL database (`ficol_db`).

Historical test suites and attribution:
- **Historical database-free unit selection**: previously passed 50 tests across location validation, pure offers, rolling allowance, release requests, document access, and payment validation.
- **Account information unit tests**: 70 automated unit tests executed and passed (`manage.py test apps.accounts.information.tests`) during feature development on its branch.
- **Full test baseline attribution**: an earlier run of the full test suite evaluated 294 tests, recording 2 failures and 8 errors. Features and cross-app integrations affected by these failures and errors cannot be considered fully verified until they are investigated and resolved.

Database suites must use a **disposable Django test database with PostGIS**, not live data:

```powershell
python manage.py test apps.assignments.tests apps.activities.tests apps.collection.tests apps.user.tests apps.accounts.tests
```

Use explicit `.tests` module labels; `apps.assignments` alone previously caused namespace-package discovery failure. Concurrency and transactional persistence suites remain unrun in read-only inspection sessions. A `CREATE DATABASE` permission error means the test environment is not prepared; do not point tests at the live database to work around it.

Manual verification reported by the owner includes phone location saving, nearby offer discovery/acceptance, visit submission, and release rejection/resubmission/approval. The release screen showed zero active accounts while retaining one rolling acceptance. Separate evidence-access, cancellation, concurrency, and desktop/mobile visual checks should not be inferred from that screenshot.

### PWA testing (local and HTTPS ngrok)

The PWA foundation provides an installable shell and an offline fallback page. It does not provide offline data storage, offline submissions, or background synchronization.

#### 1. Local development verification

1. Start the Django development server:
   ```powershell
   .\ficolenv\Scripts\python.exe manage.py runserver
   ```
2. Verify root PWA routes in a browser or curl:
   - `http://127.0.0.1:8000/manifest.webmanifest`: verify JSON response with `application/manifest+json` content type, `start_url: /login/`, and references to 192px and 512px PNG icons.
   - `http://127.0.0.1:8000/service-worker.js`: verify JavaScript response with `application/javascript`, `no-cache` headers, and `Service-Worker-Allowed: /`.
   - `http://127.0.0.1:8000/offline/`: verify generic branded offline fallback page with "Try again" button and zero business/customer data.
3. Open Chrome DevTools (`F12`):
   - **Application > Manifest**: verify identity, icons, theme/background colors (`#1D4ED8` and `#0B192C`), and `standalone` display mode.
   - **Application > Service Workers**: verify `/service-worker.js` is registered with scope `/`.
   - **Application > Cache Storage**: verify `ficol-pwa-v1` caches only `/offline/` and safe static icon assets.

#### 2. HTTPS ngrok installation testing (Desktop & Mobile)

Service workers and PWA installation require a secure origin (HTTPS or localhost). To test installation on mobile devices or Chrome desktop through a development tunnel:

1. Start the Django development server:
   ```powershell
   .\ficolenv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
   ```
2. Start the HTTPS ngrok tunnel:
   ```powershell
   ngrok http 8000
   ```
3. Configure `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` in `.env` to include your ngrok hostname:
   ```dotenv
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],your-tunnel.ngrok-free.app
   DJANGO_CSRF_TRUSTED_ORIGINS=https://your-tunnel.ngrok-free.app
   ```
4. **Important**: Both Django and ngrok must remain actively running while using the installed PWA. Because this foundation provides only an installable shell and offline fallback without offline business data, attempting to navigate beyond the offline fallback while the server is stopped will simply show the offline screen.
5. Open the live HTTPS ngrok URL in Chrome on desktop or mobile:
   - **Desktop**: Click the "Install app" icon in the Chrome omnibox (address bar) or choose "Install FiCol..." from the Chrome menu.
   - **Android**: Tap the "Install application" prompt or select "Add to Home screen" / "Install app" from Chrome's menu.
6. Verify service worker update behavior:
   - When a new service worker version is waiting, the accessible `#pwa-update-notice` banner appears with "Reload to update".
   - The reload only occurs after controller change and avoids interrupting active form entry.
7. Verify offline fallback:
   - In Chrome DevTools, check "Offline" in the Network tab (or turn on Airplane mode on a mobile device).
   - Navigate to an unvisited page; verify the generic FiCol offline fallback page appears with the "Try again" action.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No offers / client-access notice | Collector and client must both have active `ClientAccess` scope. Account existence alone does not grant access. |
| Update-location notice | Save a new location, then refresh offers. A fix older than the client's window prevents matching. |
| Ready but zero offers | Check account distance, active ownership, source allowlist, verified address, earning terms, client policy, and pending invitations. Do not insert more records before diagnosing the exclusion. |
| Location POST returns HTTP 400 | Inspect the actual Network payload and response: `accuracy_m`, `captured_at`, `error.code`, and `error.message`. Do not infer the cause from the HTTP status alone. |
| Laptop pin looks correct but location is rejected | Inspect reported uncertainty and observation age; a visually correct pin does not establish an acceptable accuracy value. |
| GDAL/GEOS library error | Verify native DLL paths and dependent libraries; check GIS environment overrides. |
| Tailwind classes missing after edits | Run the frontend build from the project root. Check content paths and complete dynamic class names. |
| Browserslist data warning | Distinct from build failure; inspect the build's exit/result. Dependency-data updates are a separate deliberate change. |
| Many Network entries | Map tiles and browser extensions add requests; filter by the application host. HTTP 200 and disk-cache entries are not errors. |

## Collaboration and change boundaries

- Work on separate Git branches and review changes before merging. Coordinate shared models, migrations, routes, navigation, and stylesheets.
- For this project, get owner approval for the exact file list before creating, editing, deleting, moving, or renaming files unless that specific work is already authorized.
- Approval must include generated outputs: npm installs, CSS builds, migrations, and other generators create/update files too. Do not create unsolicited helper, scratch, preview, or backup files.
- Generate new migrations through Django. Never rewrite applied migrations, silently apply migrations, seed records, or grant permissions without authorization.
- Keep Python dependencies in `requirements.txt` and Node dependencies in `frontend/package.json` / `package-lock.json`.
- Keep internal IDs and implementation jargon out of product-facing wording; use account references and readable names. Retain internal identifiers where required for secure application behavior.
- Preserve the separation between visit reporting, scheduling, payment verification, ownership, and compensation.

The local `docs/database-architecture-proposal.md` and `docs/database-implementation-checklist.md` contain detailed planning/history but are currently ignored by Git. Share an approved copy with collaborators if needed. Historical checklist entries can lag later code changes; inspect current code and record actual verification. Original business documents and answered questions are not bundled in this repository; this README summarizes decisions rather than replacing them.

## Before production deployment

The current configuration still has development settings, including `DEBUG=True`, a development secret key, local/tunnel host settings, local GIS paths, and no completed production static/private-storage deployment configuration. Compiled Tailwind removes the development CDN dependency; it does not make the entire application deployment-ready.

Before launch, configure secrets, HTTPS, allowed hosts/CSRF origins, a production WSGI/ASGI server, static collection/serving, protected document storage, database backups/restores, logging/monitoring, and an appropriate map service. Run deployment checks against the intended production settings and complete database/concurrency and end-to-end role/access tests.

Do not commit `.env`, virtual environments, `node_modules/`, private uploads, or customer data. Current `.gitignore` excludes `.env`, Node dependencies, and the two local database documents; virtual environments and private uploads still need an approved ignore-rule review. No ignore rules are changed by this README.

Remaining product work includes approved points/compensation rules, automated inactivity/missed-task transfer, financial balance reconciliation/closure controls, and CRM integration. While manual assignment and information validation are implemented in code and migrated locally, end-to-end operational verification and resolution of test failures remain required. Track these as separate reviewed batches; do not treat planned features as completed.

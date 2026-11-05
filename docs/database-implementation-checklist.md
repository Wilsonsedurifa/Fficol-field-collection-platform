# FiCol database implementation checklist

Last reviewed: 18 October 2026
Priority: Collector portal first
Architecture baseline: docs/database-architecture-proposal.md (v2.4 reconciliation; answered business-process baseline dated 13 October 2026).

## How to maintain this checklist

- This is the local progress record for every authorized database implementation batch.
- Update model, migration and verification status after each batch; record the date and actual checks performed.
- Check a model's main checkbox only after its reviewed definition and migration exist, the migration is applied to the named target database, and relevant constraints/relationships are verified.
- A model definition or migration file alone does not prove a database table exists. Do not mark inaccessible databases as verified.
- Identify development versus production verification. Development completion does not imply production deployment.
- Keep deferred business policies visible; development values are provisional until approved.
- Creating tables does not complete collector workflows. Track backend and portal integration separately below.
- This checklist does not authorize code edits, migrations, database writes, or deployment. Obtain approval for the relevant implementation batch.
- Do not store credentials, connection strings, customer data or payment evidence here.

## Revised business baseline — documentation only

Source: answered process questionnaire. This section records governing business rules; current implementation and verification are reconciled below. Updating this document itself changes no code or database.

- [x] Record CRM-admin collector approval, 24-hour inactivity/reassignment, admin-approved release and admin-controlled follow-ups as supplied process rules.
- [x] Record Back Office review of visit reports; report rejection prevents remuneration eligibility, without silently reversing customer payments.
- [x] Record that FC cannot receive customer money; customer payment assistance/proof reporting is separate from company-funded collector remuneration.
- [x] Record client-specific remuneration for valid information; actual rates and payout timing remain unconfirmed.
- [x] Record one point each for admin-validated address, email, phone, contact person and payment arrangement.
- [x] Owner approved multiple assigned accounts with only one ongoing visit per collector.
- [x] Owner approved ten new offer acceptances per rolling 24 hours; implementation recorded below.
- [ ] Clarify qualifying inactivity resets and future appointment handling.
- [ ] Clarify proof for non-payment/unavailable visits, GPS failure versus wrong address, and scheduled availability versus missed deadline/grace.
- [ ] Verify Back Office visit review on the local database/phone.
  - [x] New submissions use pending; scoped approve/reject queue, reasons, audit and retry protections implemented.
  - [ ] Manually configure reviewer permission/client access and run PostgreSQL review/concurrency tests. Legacy reports remain unchanged.
- [x] Collector follow-up dates are requests; Back Office scheduling supplies earliest-start/deadline authorization. Historical null-authorization schedules retain compatibility behavior; full DB/device checks remain separately pending.
- [x] Add remote call/email/SMS recording; internal Call, SMS, and Email logging is implemented and sends no external messages.
- [x] Structured information validation (five categories: address, email, phone, contact person, payment arrangement) and Back Office validation workflow implemented in apps/accounts/information; migration 0006 applied; 70 tests passed.
- [x] Implement collector release request and Back Office review workflow (AssignmentReleaseRequest); ownership preserved while pending; migration 0004 generated.
- [ ] Implement race-safe inactivity/missed-task transfer and separate allocation limits only after unresolved semantics are confirmed.
- [x] Implement customer payment reporting with private evidence; payment verification remains independent.
- [x] Extend rates/earnings for validated information: client-specific rates, commission snapshots on proposal approval, and company payouts implemented in apps/collection; migration collection 0005 is owner-reported applied to ficol_db; it was not independently rechecked in this current session.
- [x] Implement verified activity points (Stars & Performance): 1 point awarded per Back Office approved information proposal under an active PointRule; PointRule and PointTransaction implemented in apps/collection; migration 0006 generated; application to database pending.
- [x] Implement approved superuser-only System Admin portal: manages users, client operational status, ClientAccess assignments, and Back Office capabilities using existing models (User, CollectorProfile, Client, ClientAccess, AuditEvent, Permission); requires no migration; does not modify policy settings or send emails.
- [ ] Apply and operationally verify the implemented admin-authorized account lifecycle workflow. Code and migration `accounts: 0007_accountstatuschange_alter_account_options_and_more.py` are present in this merge, but database application and live verification remain pending. `resolved` and `closed` require a `0.00` source balance snapshot and no unverified payment reports; WIRED CRM live reconciliation remains deferred.

Do not overwrite applied migrations, invent historical approvals, seed guessed production rates, or treat configuration as policy approval. Original requirements remain context; the answered questionnaire and architecture Sections 0/18 define the revised target. See architecture Section 18 for ordered batches and proposed files. Documentation approval does not authorize application or database changes.

## Current verified position

Code inspection identifies 30 domain model definitions: Client, Customer, CustomerAddress, Account, AccountStatusChange, FieldActivity, FollowUpTask, Document, CollectorLocation, Territory, AccountOffer, AccountAssignment, AssignmentReleaseRequest, AuditEvent, CommissionRule, CommissionTier, PromiseToPay, Payment, Notification, User, CollectorProfile, ClientAccess, the information validation models CustomerContact and AccountInformationProposal, the finance models InformationEarningRule, Commission, Payout, and PayoutItem (migration 0005 is owner-reported applied and not independently rechecked in this session), and the verified points models PointRule and PointTransaction (migration `0006` generated, unapplied to database). `AccountStatusChange` is introduced by generated, unapplied migration `accounts: 0007_accountstatuschange_alter_account_options_and_more.py`. Only two models from the original proposal remain unimplemented: CollectorServiceArea and IntegrationEvent. Django framework/intermediary tables (7 framework models and 3 M2M join tables) are outside this count.

A historical database inspection in PostgreSQL (`ficol_db`) performed before migrations 0005 and 0006 verified the earlier 43-migration / 23-domain-table state (25 application migrations across 7 custom apps, 18 Django framework migrations). The current session cannot independently query PostgreSQL; migration 0005 is owner-reported applied and was not independently rechecked in this session; migration 0006 remains generated and unapplied.

Test execution status and attribution:
- 50 database-free unit test selection historically passed.
- 70 unit tests executed and passed during account information development (`manage.py test apps.accounts.information.tests`).
- 59 database-free unit and template rendering tests passed for System Admin, portal routing, and audit allowlist suites.
- The known full-suite baseline from an earlier test run evaluated 294 tests, recording 2 failures and 8 errors. Features affected by these failures and errors cannot be considered fully verified until resolved.
- In current read-only checks, `python manage.py check` passed with zero issues (0 silenced). Concurrency, transactional persistence, and end-to-end operational workflow tests remain pending on a disposable PostGIS test database.

Manual Back Office account assignment/reassignment and Superseded release status are implemented in code and migrated locally (`assignments: 0005_alter_accountassignment_options_and_more` applied). AccountAssignment includes `event_id` and permission `manage_account_assignments`. AssignmentReleaseRequest supports `superseded` status with updated constraints. Management views and routes are registered; live operational verification and concurrency test runs remain pending.

Collector account-information proposals and Back Office validation are implemented in code and migrated locally (`accounts: 0006_account_information` applied). Dedicated permission `accounts.validate_account_information` is active. The implementation is committed on local `main` and on remote tracking branch `origin/feature/pito-information-validation` (based on locally recorded refs); integration into `origin/main` remains pending.

## 1. Existing foundation

- [ ] User — model defined; user/0001_initial.py applied to target database; table exists; constraint/relationship and workflow verification pending.
- [ ] CollectorProfile — model defined; user/0001_initial.py applied to target database; table exists; constraint/relationship and workflow verification pending.
- [ ] Client — model defined; accounts/0001_initial.py applied to target database; table exists; constraint/relationship and workflow verification pending.
- [ ] ClientAccess — model defined; user/0001_initial.py applied to target database; table exists; constraint/relationship and workflow verification pending.

## 2. Collector readiness and nearby accounts

- [ ] AuditEvent — model defined; audit/0001_initial.py applied; table exists; redacted append helper and tests implemented; constraint/persistence verification pending.
- [ ] CollectorLocation — model defined; assignments/0001_initial.py applied; table exists; definitions exist in model/migration; runtime spatial index/constraint and concurrency verification pending.
  - [x] Model reviewed against v2.1, including one latest observation per collector and indexed WGS84 geography.
  - [x] Django-generated migration matches saved models; system check passed.
  - [x] Database-free checks passed for one-to-one/CASCADE, point field configuration, accuracy validation and server timestamp configuration.
  - [x] Migration applied to target database; physical table verified.
  - [ ] Verify concurrency and spatial distance behavior under load on the approved target database.
- [ ] Customer — model defined; accounts/0002_customer.py applied; table exists; field/relationship validation checked; full concurrency verification pending.
  - [x] Model fields and constraints reviewed against architecture v2.1.
  - [x] Migration generated by Django; saved model/migration state matches.
  - [x] Django system check and database-free field/default/relationship checks passed.
  - [x] Migration applied to target database; physical table verified.
  - [ ] Verify scoped uniqueness and foreign key constraints under concurrent writes on the approved target database.
- [ ] CustomerAddress — model defined; accounts/0003 and 0004 applied; table exists; coordinate and review validation implemented; full workflow verification pending.
- [ ] CustomerContact — model defined in apps/accounts/information/models.py; accounts/0006_account_information.py applied; table exists; 70 unit tests historically reported passing during feature branch development; full constraint/workflow verification pending.
- [ ] AccountInformationProposal — model defined in apps/accounts/information/models.py; 5 categories; accounts/0006_account_information.py applied; table exists; 70 unit tests historically reported passing during feature branch development; full workflow verification pending.
- [ ] Account — model defined; accounts/0003 and 0004 applied; table exists; client/customer/address/rule consistency checks implemented; full workflow verification pending.

## 3. Offers and assigned work

Commission policy implementation depends on approved business requirements. Financial terms are required before offers become actionable.

- [ ] CommissionRule — model defined; collection/0002_initial.py applied; table exists; percentage/fixed/tiered configuration checks implemented; operational workflow verification pending.
- [ ] CommissionTier — model defined; collection/0002_initial.py applied; table exists; exclusion constraint plus coverage validation implemented; collection/0001_enable_btree_gist.py applied; operational workflow verification pending.
- [ ] AccountOffer — model defined; assignments/0003 applied; table exists; private summary, matching, response/expiry checks implemented; full concurrency verification pending.
- [ ] AccountAssignment — model defined; assignments/0003 and 0005 applied; table exists; atomic acceptance, exclusive-owner constraint, rolling-allowance serialization, and manual assignment/reassignment with event_id implemented; full concurrency verification pending.
- [ ] AssignmentReleaseRequest — model defined; assignments/0004 and 0005 applied; table exists; request/review workflow, superseded lifecycle, and review conflict protections implemented; full concurrency verification pending.
- [ ] Notification — model defined; core/0001_initial.py applied; table exists; deduplicated event records created transactionally; delivery implementation pending.

## 4. Visits and follow-ups

- [ ] FieldActivity — model defined; activities/0001, 0002, 0003, 0007 applied; table exists; collector workflow implemented; full review/concurrency verification pending.
- [ ] Document — model defined; activities/0005 and 0006 applied; table exists; private storage/upload validation/streaming implemented; live file-access/concurrency verification pending. Original uploader identity is not an access bypass.
- [ ] FollowUpTask — model defined; activities/0001 and 0004 applied; table exists; collector workflow implemented; full scheduling/concurrency verification pending.
- [ ] PromiseToPay — model defined; collection/0003_promisetopay.py applied; table exists; collector workflow implemented; full workflow verification pending.

## 5. Payments, earnings and performance

- [ ] Payment — model defined; collection/0004_payment.py applied; table exists; reporting/verification services implemented; lifecycle and client/reviewer guards tested; independent persistence/constraint verification pending. No balance/ownership/reward side effects.
- [ ] InformationEarningRule — model defined in apps/collection/models.py; collection/0005_information_earnings_and_payouts.py owner-reported applied to database (not independently rechecked in this session).
- [ ] Commission — model defined in apps/collection/models.py; collection/0005_information_earnings_and_payouts.py owner-reported applied to database (not independently rechecked in this session).
- [ ] Payout — model defined in apps/collection/models.py; collection/0005_information_earnings_and_payouts.py owner-reported applied to database (not independently rechecked in this session).
- [ ] PayoutItem — model defined in apps/collection/models.py; collection/0005_information_earnings_and_payouts.py owner-reported applied to database (not independently rechecked in this session).
- [ ] PointRule — model defined in apps/collection/models.py; collection/0006_verified_points.py generated (enforces pointrule_exactly_one_point); unapplied to database.
- [ ] PointTransaction — model defined in apps/collection/models.py; collection/0006_verified_points.py generated; unapplied to database.

## 6. Later integration and conditional features

- [ ] IntegrationEvent — model pending; migration pending; database verification pending.
- [ ] Territory — optional lookup model defined to preserve CustomerAddress.territory; assignments/0002_territory.py applied; table exists; territory eligibility restrictions unactivated.
- [ ] CollectorServiceArea — conditional on company requirements; not implemented.

## 7. Prerequisites and existing-model additions

- [ ] Verify installed Django/database versions and applied initial migrations using an authorized local connection.
- [ ] Confirm intended development database and preserve existing data before schema changes.
- [ ] Verify PostgreSQL/PostGIS extension, GeoDjango backend and compatible native runtime dependencies before spatial migrations.
  - [x] Runtime SQL inspection on ficol_db confirms PostgreSQL 18.6 and PostGIS 3.6.2 (historical pgAdmin report had noted PostgreSQL 17 and PostGIS 3.5.3).
  - [x] Configured GeoDjango PostGIS backend and django.contrib.gis in config/settings.py.
  - [x] Verified installed Windows GDAL/GEOS DLLs load through project settings; GDAL 3.9.2 and WGS84 Point creation succeeded.
  - [x] Django system check passed; saved model/migration state unchanged by settings update.
  - [x] Owner supplied successful Django SQL output querying ficol_db, PostGIS version and a WGS84 point.
  - [ ] Verify spatial database round-trip and migration behavior using the approved local connection before declaring end-to-end readiness.
- [ ] Review and implement proposed Client policy/version and GPS/matching configuration fields in an approved batch.
  - [x] Planned fields added; accounts/0005 generated. Defaults remain provisional.
  - [x] Migration accounts: 0005 applied in ficol_db; physical columns exist; runtime constraint and operational policy verification pending.
- [ ] Approve dispatch limits, capacity, offer expiry and ownership policy values before live use.
- [ ] Approve payment verification, commission/payout adjustments and reward policies before enabling related workflows.
- [ ] Approve GPS/evidence retention and actual WIRED data contract.

## 8. Collector portal workflow completion

These checkboxes track complete working flows, not visual previews or schema creation.

- [ ] Protected availability changes with audit and approval checks.
  - [x] Approved-collector Online/Offline service, CSRF-protected endpoint and offers-page control implemented.
  - [ ] Verify database writes and browser flow locally.
- [ ] Server GPS submission, validation, freshness and out-of-order handling.
  - [x] Backend implemented at POST /collector/location/; signed-in active approved collector required; identity derived from session; CSRF protection retained.
  - [x] Provisional configurable submission limits: age 300 seconds, accuracy 50 metres, future clock skew 120 seconds. These are not approved client matching policies.
  - [x] Parent-profile locking and transactional latest-location writes implemented; exact retries preserve receipt time; older/conflicting fixes return 409.
  - [x] Sixteen database-free validation/endpoint tests passed; Django check passed; no model/migration changes needed.
  - [ ] Run seven database-backed persistence/concurrency tests locally against Django's separate PostGIS test database. Agent execution was blocked before database setup by connection permissions.
  - [x] Owner approved direct frontend integration in collector.html/collector.js and confirmed successful saving during phone testing through ngrok. Automated persistence/concurrency checks remain pending.
- [ ] Fictional nearby/far account fixtures in a separate development database.
- [ ] Nearby matching and protected offer summaries.
  - [x] PostGIS radius/distance queries, explicit ClientAccess eligibility, nearest/fan-out selection and repeat-safe invitations implemented. Exact account coordinates withheld in pre-acceptance responses.
  - [ ] Verify against development accounts after migration; no accounts seeded by agent.
- [ ] Atomic acceptance, one active owner and capacity enforcement.
  - [x] Services, constraints, retry handling and tests implemented.
  - [ ] Run PostgreSQL acceptance/concurrency tests locally.
- [ ] Assigned account list/details with client and ownership access checks.
  - [x] Protected My Accounts/detail routes, templates, exact assigned pin and Google Maps directions link implemented.
  - [ ] Verify with a real development assignment.
- [ ] Visits, private evidence and follow-up scheduling.
  - [x] Collector visit start/resume/submit/cancel, GPS evidence/reasons, promise and follow-up scheduling, and per-assignment history implemented.
  - [ ] Apply activities/0001_initial and collection/0003_promisetopay, run persistence/concurrency tests and verify on phone.
  - [ ] Implement supervisor review, private document evidence and follow-up completion/transfer controls.
- [x] Payment reporting with required independent verification controls.
  - [x] Non-custodial payment reporting by active collector with private proof-of-payment upload (PDF/JPEG/PNG/WebP, 100B - 5MB max) to private storage.
  - [x] Client UUID event_id deduplication and submission fingerprinting.
  - [x] Back Office verification queue with pending/verified/rejected tabs, client access scoping, no self-review (`reported_by != reviewer`), and mandatory rejection reason.
  - [x] Private evidence streamed via authenticated permission-checked endpoint (`/collector/documents/<document_id>/download/`).
  - [x] Reporting and verification are strictly non-custodial: balance, closure, promises, and commissions remain untouched.
  - [x] 18 database-free tests passed.
- [ ] Verified commissions and authorized payout summaries.
- [ ] Verified points, deduplication, reversals and Manila daily caps.
- [ ] Notifications and accurate unavailable/empty states.
- [ ] Relevant concurrency, permission and retry tests against PostgreSQL/PostGIS.

Prioritize collector flows, while providing the minimum management approval/review controls required by each enabled operation. Supervisor portal polish can follow; required review cannot be skipped.

## 9. Implementation history

| Date | Authorized batch | Model/migration result | Target database and verification | Remaining work |
|---|---|---|---|---|
| 2026-10-10 | Initial checklist creation | Four existing definitions and initial migration files identified; no model changes | No live database verification or writes performed | Verify initial schema; review AuditEvent and CollectorLocation batch |
| 2026-10-10 | Customer model batch, approved after architecture review | Added Customer to apps/accounts/models.py; Django generated accounts/0002_customer.py; existing Client and initial migration preserved | Django check passed; migration state, field/default validation, PROTECT relationship and scoped uniqueness definition checked without DB access. PostgreSQL connection denied; no migration applied or records inserted | Apply and verify on an approved database; resolve spatial/dependent-model prerequisites before Account and AuditEvent |
| 2026-10-10 | Customer migration owner update | Owner reported applying the generated Customer migration | Application reported; table/constraint checks not independently run | Verify actual schema and migration state |
| 2026-10-10 | Approved GeoDjango configuration | Updated config/settings.py to PostGIS backend, enabled GIS app and configured Windows DLL loading with environment overrides; no new migrations | Owner screenshot confirms enabled PostGIS; local project settings load GDAL 3.9.2/GEOS and create a WGS84 Point; Django check and offline migration-state comparison passed. No DB connection or writes performed | Verify local spatial database round-trip; approve CollectorLocation model batch |
| 2026-10-10 | Owner PostGIS connection verification | No model or migration changes | Owner's Django shell SQL returned ficol_db, PostGIS 3.5 and the expected WGS84 point | Verify spatial model persistence and schema after approved migration application |
| 2026-10-10 | Approved CollectorLocation model batch | Added apps/assignments/models.py definition; Django generated assignments/0001_initial.py depending on user/0001_initial | Django check passed; offline migration-state comparison and geography/accuracy/relationship/timestamp checks passed. History check could not connect to local PostgreSQL; no migration applied or records inserted | Apply and verify schema locally; implement protected GPS submission, freshness and out-of-order handling in a separately approved batch |
| 2026-10-12 | CollectorLocation owner migration update | Owner reported applying assignments migration | Application reported; actual table/index/constraints not independently queried | Verify database schema and persistence |
| 2026-10-12 | Approved seven-file GPS backend batch | Created assignments/services.py and urls.py; edited assignments/views.py and tests.py, config/urls.py and settings.py, and this checklist. No schema changes | Sixteen validation/endpoint tests and Django check passed; offline migration-state comparison passed. Seven persistence/concurrency tests could not start because local PostgreSQL connection was denied; no agent database writes occurred | Run local PostGIS tests; then approve frontend integration. No claim that nearby offers or availability changes work yet |

| 2026-10-12 | Owner-approved account foundation after file creation | Six models: Territory, CustomerAddress, Account, CommissionRule, CommissionTier, AuditEvent; validation/audit helpers; Django PostgreSQL app enabled; migrations generated | Seventeen new database-free foundation tests plus sixteen GPS regression tests passed (33 total); system check, offline migration-state/dependency checks and exclusion SQL compilation passed. No live migration application or fixture insertion by agent; database-backed checks still pending | Apply full migration graph, then verify account persistence/tier exclusion. Map page, Online/Offline, offers and assignment models/services are next |

| 2026-10-12 | Owner migration confirmation: account foundation | All six newly generated migrations displayed OK in owner terminal screenshot | Extension/table creation confirmed by supplied migration output; individual constraints not independently exercised | Preserve approved schema; verify integration on local PostgreSQL |
| 2026-10-12 | Approved map offers and acceptance batch | Client policy fields; AccountOffer, AccountAssignment, Notification; new migrations; availability/matching/response services; protected pages/APIs; Leaflet renderer and responsive collector templates/assets; sidebar links | 39 database-free tests passed; Django checks, migration-state and spatial-query compilation passed; all pages rendered including Manila dates; simulated JS flows passed. Full suite discovers 64 tests: 25 require PostgreSQL and were not run here. No migrations applied or account records inserted by agent | Apply migrations, confirm development database/client grants and seed separately approved fictional accounts; perform browser and database/concurrency verification |
| 2026-10-14 | Private evidence uploads and customer payment reporting/verification batch | Added Payment (collection/models.py) and Document (activities/models.py); migrations collection/0004_payment.py, activities/0005_document.py, activities/0006_document_payment_document_uploaded_by_and_more.py applied; validation services, private media storage, collector form & AJAX submission, Back Office verification queue, permission-checked document download | 18 database-free tests passed (PaymentModelValidationTests, DocumentUploadValidationTests, PaymentReportingValidationTests, PaymentReviewValidationTests, DocumentAccessPermissionTests); Django check passed; migration graph applied cleanly to target database. No customer money handled by FiCol; non-custodial; no balance/promise/commission side effects | Browser and device verification of payment evidence upload on live network |
| 2026-10-14 | Collector release-request workflow batch | Added AssignmentReleaseRequest (apps/assignments/models.py); generated migration apps/assignments/migrations/0004_assignmentreleaserequest.py; collector request UI & service, Back Office review queue (/back-office/release-requests/), lock hierarchy (account -> collector_profile -> assignment -> client/grants -> request/tasks), ongoing visit gating, follow-up task cancellation, audit trailing, release-only reviewer portal entry, private evidence former-owner access denial, rolling acceptance preservation, post-approval retry acknowledgment without account reactivation, locked grant and profile rechecks, local portal.css stylesheet classes without Tailwind CDN reliance | 24 database-free unit tests passed (ReleaseRequestModelValidationTests, ReleaseRequestPayloadValidationTests, ReleaseRequestDecisionValidationTests, ReleaseRequestPermissionTests, ReleaseRequestWorkflowPureTests, RollingAllowancePureTests, DocumentAccessPermissionTests); Django check passed (0 silenced). 6 database and concurrency tests (ReleaseRequestDatabaseTests, ReleaseRequestConcurrencyTests) written in apps/assignments/tests.py but unrun due to PostgreSQL CREATE DATABASE permission restrictions | Migration application and live browser/PostgreSQL verification pending |

For each later batch, add a row with the affected models, migration filenames, database environment and actual verification results. Do not report checks that were not run.

## 10. GPS backend contract for later frontend integration

Route name: assignments:submit_location. URL: /collector/location/. POST with application/json, the signed-in session and a valid X-CSRFToken header. Submit only these fields:

```json
{
  "longitude": 120.9842,
  "latitude": 14.5995,
  "accuracy_m": 18.25,
  "captured_at": "2026-10-12T04:00:00.000Z"
}
```

The timestamp above is illustrative, not a reusable live observation. Use new Date(position.timestamp).toISOString() from the actual browser observation, not the later request time. Numbers must be finite JSON numbers; collector identity cannot be supplied by the browser. Never fabricate a successful save.

On HTTP 200, the JSON response includes saved, changed, location (longitude, latitude, accuracy_m, captured_at, received_at), server_time, age_seconds, fresh_for_submission and matching_eligibility="not_evaluated". changed=false denotes an identical observation already saved; it does not refresh receipt time. Current submission validation still applies on retries; expired observations must be recaptured. Freshness refers to these provisional submission limits, not per-client offer eligibility. No internal collector or row IDs are returned.

Controlled errors use {"error": {"code": "...", "message": "..."}}. Codes cover invalid_observation/invalid_json/stale_location/inaccurate_location/future_location (400), authentication_required (401), collector_required/approval_required (403), older_observation/conflicting_observation (409), payload_too_large (413), unsupported_media_type (415) and method_not_allowed (405). CSRF rejection is Django's normal 403 response and may be HTML; the frontend must handle non-JSON failures and network errors without showing saved success.

Explicit manual GPS submission is allowed for an approved collector while offline; it does not change availability, start background tracking, release assignments or grant offers. This behavior is provisional pending company policy approval. No cleanup/history collection, AuditEvent table, offer generation or frontend changes are implemented by this batch.

Local verification command (Django creates a separate test database; do not configure TEST.NAME to the operational database):

```powershell
python manage.py test apps.assignments --noinput
```

The full suite contains 23 tests: 16 database-free tests already passed here; seven require PostgreSQL/PostGIS and remain unverified in this session.


## 11. Account-foundation migration and verification batch

New migration files (Django-generated models; the empty extension migration was generated by Django, then supplied with BtreeGistExtension as the reviewed prerequisite):

- assignments/0002_territory.py
- collection/0001_enable_btree_gist.py
- collection/0002_initial.py
- accounts/0003_customeraddress_account.py
- accounts/0004_account_commission_rule_account_customer_and_more.py
- audit/0001_initial.py

Apply the complete graph, not just one app: python manage.py migrate. This changes the approved target database and is not executed by this documentation update. No existing applied migration file was modified. If extension creation fails for the application role, an administrator must enable btree_gist in the correct database; do not grant superuser to the application role as a workaround.

Checks passed: 17 new SimpleTestCase tests across accounts, collection and audit plus the existing 16 GPS database-free tests; no model/migration drift; the extension operation precedes the commission exclusion constraint. Numeric ranges use the existing lower/upper columns with NULL unbounded top bands. A separate service checks gapless coverage from zero to an unbounded upper range for the proposed supported domain.

Seven new foundation database tests are implemented but not run in this session: four account foundation tests, two commission database tests and one audit append test. They require a separately created PostGIS test database. The prior seven GPS database tests also remain pending. Manual GPS success is useful evidence but does not validate every constraint or concurrency scenario.

Account clients must match customers; designated addresses must belong to the customer; earning-rule client and currency must match the account. Model clean/service validation enforces these cross-table rules; ordinary raw saves/SQL do not automatically run full_clean. Future import, admin and offer workflows must call these validators and check permissions. They are not enforced by foreign keys alone. No authenticated account-write API, publication/edit workflow or matching engine is delivered by this batch.

Published/referenced financial rules must remain immutable through the later management/offer workflows. This foundation provides configuration validation and overlap protection, not complete publication permissions. Audit helpers allow only bounded named evidence fields; database-role controls and approved retention are still required for stronger append-only guarantees.

No fictional debts, addresses, balances or rates were inserted into ficol_db. Test fixtures exist only in automated test code for a disposable test database. Territory classification is optional and never substitutes for GPS or silently restricts matching. CollectorServiceArea is still deferred.


## 12. Map offers/acceptance implementation and operator prerequisites

New migrations: accounts/0005_client_allow_collector_release_and_more.py; assignments/0003_accountoffer_accountassignment_and_more.py; core/0001_initial.py. Apply with python manage.py migrate on the approved target database. No applied migration was edited by this batch.

Routes: /collector/offers/ (map page), /collector/offers/data/ (read-only JSON), /collector/offers/refresh/ (POST matching), /collector/availability/ (POST JSON online/offline), /collector/offers/<offer_id>/accept/ and decline/ (POST), /collector/accounts/ and /collector/accounts/<assignment_id>/. All mutations retain CSRF protection; page/API access requires an active approved collector.

Explicit ClientAccess grants are required for collectors in this initial implementation, using the existing user/client relationship. They do not grant finance permissions. This conservatively restricts scope while company-wide collector eligibility remains unresolved; the company must confirm the policy before live use. No grants are automatically inserted. Customer/account scope and active client are rechecked, including on assignment-detail access.

Nearest dispatch chooses one eligible closest collector; fan_out uses max_offer_recipients (validated technical maximum 20); manual dispatch generates no automatic invitations. Current valid pending invitations occupy recipient slots until expiry or invalidation; active ownership is never displaced by a new/closer collector. Declined invitations are not reissued before their original expiry. Territory lookup is optional; require_service_area_membership=True blocks this workflow until the deferred service-area support is implemented. Release/reassignment flags are stored but no release/reassignment worker is enabled.

Matching is request-driven and processes up to 40 nearby candidate accounts per refresh; offer lists return up to 60 pending summaries. Pagination/scheduled dispatch may be needed for production volumes. Matching uses frozen WGS84 geography points and approved/provisional client limits. Acceptance locks Account, CollectorProfile, then affected AccountOffer rows in PK order; dependent location/client/address/rule locks follow. Transaction retries are bounded for SQLSTATE 40001/40P01; invitations, audit events and notification records are written atomically. External delivery is not performed inside transactions.

Offer terms/policy/coordinates are compared before acceptance; changed data requires refresh/re-offer. An exact successful acceptance retry returns the existing active assignment. Ownership, scope, GPS, capacity and expiry are rechecked; a late expiry worker cannot permit an expired offer. Partial unique constraints protect one pending invitation per recipient/account and one active assignment per account.

Pre-acceptance responses contain operational reference, generic/territory area, distance rounded to 100 metres, earning terms, expiry and a stable approximate grid-area marker; no customer name, full address or exact target coordinates. Post-acceptance and protected detail responses disclose the assigned location. Leaflet rendering is isolated in static/js/components/map.js; tile URL/attribution are configurable. Leaflet 1.9.4 is pinned through CDN URLs with integrity checks. OSM attribution stays visible; no offline tile downloads or bulk prefetching. Use a production-suitable tile provider before launch.

Directions are a Google Maps URL to the frozen assignment destination and open externally; FiCol does not provide embedded road routing, traffic or turn-by-turn navigation. Acceptance returns confirmed ownership before exposing the exact pin or enabling directions. Customers are not geocoded automatically from text-only addresses in this batch.

Map UI includes desktop offer list/cards and mobile bottom sheet; actions display actual request outcomes and controlled errors, preserve a list fallback on map failures, show expiry and guard duplicate clicks. Existing dashboard components/styles and GPS code were preserved; Online/Offline controls are available on the new offers page. Three collector page templates and their assets are implemented. No automatic page-load geolocation or background tracking is added.

Verification: 39 SimpleTestCase tests passed (existing foundation/GPS plus six offer/privacy/API tests). The total suite discovers 64 tests across assignments/accounts/collection/audit; 25 database-backed tests, including 11 new offer/acceptance tests, need the owner's local PostGIS connection. Template and simulated DOM checks passed but do not constitute a live browser/database acceptance test. Run python manage.py test apps.assignments.tests apps.accounts.tests apps.collection.tests apps.audit.tests using a disposable PostGIS test database when permissions are configured. Manual phone testing is also required after applying migrations and approving test data.


Initial account source gate: COLLECTION_OFFER_SOURCE_SYSTEMS defaults to TEST, configurable via the environment. Matching and new acceptance are restricted to that allowlist. WIRED remains disabled for these operations until company rules and source integration are approved. Existing assigned-account access and repeat confirmation of an already accepted active assignment remain subject to ownership/client scope. This gate changes no schema and does not insert fixtures.


## 12. Collector visits — 13 October 2026

Approved batch implemented FieldActivity, FollowUpTask and PromiseToPay. Django generated activities/0001_initial.py and collection/0003_promisetopay.py; no migrations applied or account records changed by the agent. FieldActivity gains status, ended_at, location_captured_at, distance_m, location_issue_reason, cancel_reason and submission_fingerprint. One ongoing visit per assignment, coherent location evidence, lifecycle/review timestamp checks and positive promises are constrained. Cross-account relationships and authorization require service checks in addition to foreign keys.

Collector endpoints validate approved ownership and client access in a transaction. Start requests use UUID deduplication; exact completion/cancellation retries return existing results. Reports, promises, optional follow-ups and redacted audit events save atomically. GPS concern explanations are retained; no invented arrival radius. The first-action deadline and ownership remain unchanged. Offline collection, uploaded evidence, payments and reviewer/finance actions are deferred.

Verification: 53 database-free validation/endpoint/foundation regression tests passed after final service changes. Offline model/migration comparison and PostGIS distance SQL compilation passed. Ten database-backed visit tests (including concurrency) are implemented but remain unrun because this session cannot connect to local PostgreSQL. Initial/resumed/history templates render with escaped notes; simulated frontend checks passed for GPS explanations, confirmed start/resume, CSRF, conditional fields, Philippine-time follow-ups and submitted-state handling. Browser visual verification and phone testing remain required. The architecture project copy is updated; external archived documents are unchanged.


## 13. Routine visit approval removed — 13 October 2026

Owner-approved batch adds not_required as the default review state and explicitly uses it on collector submission. Ordinary reports display Submitted. Existing reviewer identity, time and reasons remain supported. activities/0002_remove_fieldactivity_activity_valid_lifecycle_and_more.py changes the lifecycle/review constraints and backfills only unreviewed submitted visits with empty review_reason. Approved, rejected and explicitly flagged pending reviews are preserved. The reverse operation restores compatibility with old constraints. This supersedes the earlier default pending-review behavior for ordinary visits.

57 database-free tests and JavaScript syntax checks passed; migration conversion/reversal was checked with in-memory records. An additional PostgreSQL preservation test is implemented but unrun. Migration application, live database verification and phone visual verification remain pending; no live record was changed by the agent. No visit waiting period, payment verification or commission approval rule was changed.


## 14. Scheduled revisit gate — 13 October 2026

Approved repeat-visit waiting rule implemented. FieldActivity.scheduled_followup links repeat visits to a revisit task; a conditional unique constraint prevents a task being reused by multiple ongoing/submitted visits. activities/0003_fieldactivity_scheduled_followup_and_more.py generated; not applied by agent. Ordinary first visits remain allowed. After submission, server-authorized next visits require the latest report's pending unconsumed revisit task to be due. Calls/promise checks do not unlock visits. Without a task, the collector schedules a future revisit through a protected endpoint. Ongoing visits can resume. Cancelled attempts leave the task pending; submitted linked visits complete it atomically and refresh next_action_due_at. Existing records are preserved and no historical attribution is guessed.

59 database-free tests passed; saved model/migration state and reservation query compilation passed; initial/waiting/unscheduled/due/ongoing templates and simulated frontend scheduling were checked. Five new PostgreSQL workflow/concurrency tests, including task reuse constraint checks, are implemented but unrun. Apply migrations and verify first submission -> scheduled wait -> due revisit -> task completion on phone. No live database changes were performed by the agent.


## 15. Answered process revision — 13 October 2026

User authorized revising the architecture and planning subsequent changes. Only this checklist and docs/database-architecture-proposal.md were edited. Architecture version 2.2 records confirmed rules, unresolved interpretations, conflicts with current code and staged implementation. Eighteen models remain defined; the proposed release-request table would extend the original target from 28 to 29. No schema or database state changed; no migration was generated/applied; no application code was modified. Historical implementation/test records remain historical, not assertions that the new business rules already work.


## 16. Back Office review — 13 October 2026

User authorized implementing Back Office review first. Edited activities services/views/tests, audit service, user views/urls/admin, existing supervisor template and collector detail template/JavaScript, plus the architecture/checklist. New submissions enter pending; authorized reviewers can approve/reject, cannot self-review or overwrite finalized decisions, and must have explicit active client scope. Reasons/reviewer/time and redacted review audit save atomically. Released-assignment history remains reviewable. Legacy not_required reports remain read-only, with no automatic enrollment. Review does not modify balances/ownership/follow-ups/promises or award money/points.

No models or migrations changed. Custom User and ClientAccess admin configuration is registered, but no live permissions/grants/groups/accounts were altered. 63 database-free tests, Django checks, JavaScript syntax, reviewed-state templates and offline model/migration comparison passed. Seven persistence/integration tests and one competing-reviewer test are written but unrun. Manual next steps: configure change_fieldactivity permission plus ClientAccess for reviewer, open /back-office/, submit a new collector visit, review it and verify collector history.


## 17. Admin-controlled follow-up scheduling — 14 October 2026

- [x] Extend existing FollowUpTask with requested/rejected states, requested_by, available_from, authorized_by, authorized_at and decision_note.
- [x] Generate activities/0004_remove_followuptask_followup_completion_evidence_and_more.py through Django; preserve applied migrations and historical rows without fabricated approval.
- [x] Both collector scheduling paths create requests instead of active schedules; new request kinds limited to revisits, with legacy completion retries preserved.
- [x] Add client-scoped Back Office scheduling, rejection and direct scheduling under change_followuptask permission; prohibit self-authorization and stale ownership.
- [x] Serialize decisions with parent locks; keep exact scheduling retries stable; redacted audit and schedule save atomically.
- [x] Separate earliest start from completion deadline; block new starts after deadline, keep ongoing visits submittable with actual late completion time.
- [x] Update collector and Back Office screens, scheduling-only login routing, request/overdue pagination and Philippine-time forms.
- [x] Django system check, JavaScript syntax and 69 database-free tests pass. Templates compile; Back Office role visibility/Philippine-time forms and collector requested/overdue/unscheduled screens render correctly. Offline model/migration comparison reports no drift.
- [ ] Apply new migration locally (agent did not apply it).
- [ ] Run new seven scheduling persistence/integration tests and competing-scheduler test with a disposable PostGIS test database; local connection blocked for this agent.
- [ ] Manual phone verification: request -> Admin schedule -> waiting -> allowed window -> submit; rejection note and overdue blocked start.
- [ ] Configure scheduling Admin with change_followuptask permission and active ClientAccess; no live permissions/grants were altered.
- [ ] Automatic reassignment remains separate pending confirmed grace and in-progress policy.


## 18. Dedicated Back Office portal — 14 October 2026

- [x] Populate the seven owner-created files: Back Office base/visits/followups templates, sidebar/navbar components, portal.css and navigation.js.
- [x] Convert existing supervisor.html into dashboard summaries; add protected Visit Reviews and Follow-up Scheduling GET routes.
- [x] Separate contexts and queries; real counts share permission/client-scope filters with the corresponding queues.
- [x] Return action successes/errors to the appropriate work page; preserve existing service/action URLs and CSRF POST logout.
- [x] Enforce feature permissions on direct pages and POST actions; hide unauthorized sidebar links.
- [x] Responsive light layout; navigation-only script supports keyboard trap, Escape, backdrop, inertness, focus restoration and resize transitions.
- [x] 74 database-free tests, Django system check, JavaScript syntax and simulated navigation interaction checks passed.
- [ ] Two new database-backed portal scope/redirect tests require a disposable PostGIS test database; agent cannot connect locally.
- [ ] Manual desktop/phone visual and actual report/scheduling form verification.
- [x] No schema migration required or generated, no migration applied, no database records/permissions/grants changed.

Next flow remains collector release requests and scoped Back Office decisions, subject to its separate approval.


## 19. Exclusive portal access — 14 October 2026

- [x] Shared Back Office classification: active superuser or review/scheduling permission (direct/group); is_staff alone is not portal authority.
- [x] Successful login always resolves through login_success; Back Office precedence over retained CollectorProfile; ordinary approved collectors route to Collector.
- [x] Deny Back Office accounts collector pages, GPS, availability, offers, visits and revisit request APIs; service guards recheck the loaded user.
- [x] Exclude privileged profiles before nearby-offer recipient limits and recheck eligibility before dispatch.
- [x] Preserve existing profiles/assignments/history and client scope. No account flags, grants or database records changed; no migration required.
- [x] 82 database-free tests and Django system checks pass.
- [ ] Four database-backed role/group/candidate tests require disposable PostGIS test database access.
- [ ] Verify on phone with separate ordinary collector and Back Office credentials; the existing superuser enters Back Office only.


## 20. Private evidence uploads and customer payment reporting/verification — 14 October 2026

- [x] Non-custodial architectural baseline: Collectors report customer payments direct to authorized creditor/agency channels; FiCol handles zero customer funds.
- [x] Zero financial side effects: Reporting or verifying a payment does NOT change account balance, close the account, fulfill promises, or generate commission/points.
- [x] Private Document evidence model (`apps/activities/models.py`) with metadata (`original_name`, `mime_type`, `size_bytes`, `sha256`) and account/assignment/payment links.
- [x] Private file storage rooted in `PRIVATE_MEDIA_ROOT = BASE_DIR / 'private_media'`; never exposed via public URL patterns or static media servers.
- [x] Upload validation: strict MIME type whitelist (`image/jpeg`, `image/png`, `image/webp`, `application/pdf`), extension checks, magic-byte inspection, file size bounds (100 B to 5 MB), and SHA-256 fingerprinting.
- [x] Payment model (`apps/collection/models.py`) with strict lifecycle status (`reported`, `verified`, `rejected`, `reversed`), foreign keys with `PROTECT`, unique constraint on `(account, source_system, external_id)`, and mandatory reasons for rejection/reversal.
- [x] Client UUID `event_id` idempotency deduplication with SHA-256 `submission_fingerprint` caching.
- [x] Reviewer authorization: requires Back Office access (`has_backoffice_access`), `collection.change_payment` permission, active `ClientAccess` scope, and prohibits self-review (`reported_by != reviewer`). Finalized decisions are immutable.
- [x] Streaming private document download view (`/collector/documents/<document_id>/download/`) with access validation requiring current approved collector ownership plus client scope, or authorized client-scoped Back Office access; no unconditional uploader bypass.
- [x] Frontend components: Collector report payment modal/form with AJAX submission in `templates/collector/components/payment_form.html`, reported payment history table in `templates/collector/account_detail.html`, and full Back Office verification queue in `templates/backoffice/payments.html` with sidebar navigation integration.
- [x] Applied migrations: `collection: 0004_payment.py`, `activities: 0005_document.py`, and `activities: 0006_document_payment_document_uploaded_by_and_more.py`.
- [x] Verification: 18 unit tests passed (`manage.py test apps.collection.tests.<Suites>`), Django system check passed with 0 errors.



## 21. One ongoing visit per collector — 14 October 2026

- [x] Partial unique FieldActivity constraint over recorded_by for ongoing visits; retain the existing assignment constraint.
- [x] Django-generated activities/0007_fieldactivity_activity_one_collector_visit.py; existing document/payment migrations preserved.
- [x] Collector-profile locking and server gate block different-account starts with controlled 409; same-account resume/UUID retry preserved.
- [x] Submit/cancel frees the work slot while multiple assignments remain owned.
- [x] Account-detail template and JavaScript display the blocked state and hide new start controls.
- [x] 83 database-free tests, JS syntax, blocked-state rendering and offline migration-state comparison passed.
- [ ] Apply migration locally; resolve any historical duplicate ongoing visits explicitly first. No automatic cancellation/deletion is performed.
- [ ] Run three persistence/constraint tests and one simultaneous different-account start test using a disposable PostGIS test database.
- [ ] Phone verification: start account A -> account B blocked -> submit/cancel A -> B can start.
- [x] Daily counting interpretation subsequently approved and implemented below; legacy capacity values remain stored but unused.


## 22. Ten new acceptances per rolling 24 hours — 14 October 2026

- [x] Owner approved counting acceptance events over rolling 24 hours, independent of active ownership and one ongoing visit.
- [x] Count offer-linked assignment creation across all clients/statuses; exact lower boundary excluded; release/completion does not refund a slot; manual assignment without offer does not consume a collector acceptance.
- [x] Enforce ten under existing collector-profile lock during acceptance; preserve accepted-offer retry behavior without duplicate counting.
- [x] Match only collectors with remaining allowance before limiting recipients; recheck under locks and withdraw exhausted pending invitations during matching.
- [x] API daily_acceptance reports used/limit/remaining/next_available_at separately from workload; snapshot the accepted policy.
- [x] Account Offers/My Accounts/dashboard show last-24-hour counts; remove five-account capacity denominator from sidebar. Legacy capacity_limit column/data retained unused.
- [x] 86 database-free tests, JS syntax, frontend quota validation, rendering and offline migration-state checks passed.
- [ ] Three new quota/history/UI database tests and one competing-last-slot test need disposable PostGIS database access.
- [ ] Phone test acceptance counts, tenth/eleventh acceptance behavior and rolling expiry.
- [x] No new migration or live data change in this batch. Prior migrations must still be applied locally.


## 23. Reconciled current status — 16 October 2026

Dated Sections 12–22 and the implementation history above retain the checks/claims recorded at the time. This section and Current verified position govern current status where historical notes conflict.

### Release-request correction and owner manual checks

- [x] Correct assignment.activities reverse relation/prefetch and ongoing-visit checks.
- [x] Preserve exact request retry after approval without restoring ownership; immutable decisions and conflicting-payload rejection.
- [x] Recheck profile approval and locked client grants; reload reviewer and refresh permission caches. No claim of detecting every concurrent role revocation.
- [x] Use local Back Office release CSS and native protected forms.
- [x] Database constraint tests use rollback savepoints and diagnostic constraint names. Isolate overlapping status constraints only within disposable-test rollback scope.
- [x] Owner reported request -> reject -> resubmit -> approve working; final screen showed zero active accounts and one rolling acceptance.
- [ ] Independently inspect the applied migration ledger/constraints on the target environment.
- [ ] Run written PostgreSQL persistence/concurrency suites on a disposable PostGIS test database.
- [ ] Explicitly verify old account/evidence URLs deny access, outstanding tasks cancel, ongoing visits block approval, and retries append no duplicate audit events. Do not infer these from the final workload screenshot.

### Frontend and dependency handoff

- [x] Tailwind 3.4.17 compiled locally; development CDN removed; templates/JavaScript scanning and default reset retained.
- [x] Login-mark favicon linked; Django static resolver supplies favicon redirect.
- [x] Location handlers display nonempty string server messages via textContent and distinguish rejection/response/network outcomes; acceptance timeout instructions preserve refresh-before-retry.
- [x] Build files and generated ignored node_modules moved under frontend/ with adjusted paths; CSS build passed after move.
- [x] Root requirements.txt pins Django/python-dotenv/psycopg[binary]; native GIS prerequisites and separate Node commands documented; pip check passed.
- [x] Root README.md records setup, roles, implemented/planned flows, troubleshooting, collaborator boundaries and production gaps.
- [ ] Actual desktop/mobile visual regression across both portals, forms and map.
- [ ] Capture actual payload/response if the prior location HTTP 400 recurs. Synthetic validation scenarios never established that browser failure's cause.

### Manual assignment/reassignment — historical plan (superseded by implementation)

- [x] Review completed for exact immutable retry metadata, stale unassigned-history token, reviewer/recipient client grants, ordinary collector role, exclusive ownership, ongoing-visit protection, frozen terms/deadlines, offer withdrawal, related-task cancellation, audit evidence and preserved allowance.
- [x] Implementation completed in `apps/assignments/services.py`, views registered in `apps/assignments/views.py` and `apps/user/urls.py`, templates created (`templates/backoffice/assignments.html`, `assignment_detail.html`), and migration `assignments: 0005_alter_accountassignment_options_and_more.py` generated and applied (commit `f252bec`).
- [x] Dedicated permission `assignments.manage_account_assignments` registered in `BACK_OFFICE_PERMISSIONS`.
- [x] AssignmentReleaseRequest updated with `superseded` status and database check constraints.
- [ ] Run automated PostgreSQL concurrency and persistence tests across competing assignments and reassignments on a disposable test database.
- [ ] Role-separated manual browser verification on live application.

### Remaining product/deployment work

- [x] Structured account-information proposals and Back Office validation; preserve original data/history (implemented in apps/accounts/information, migration 0006, 70/70 tests passed).
- [ ] Confirm inactivity resets, future appointments, missed-task grace and in-progress exceptions before automatic reassignment.
- [ ] Approved remuneration rates, point deduplication/caps/reversals, payouts and financial closure/reconciliation.
- [ ] Production secrets/settings, HTTPS/application server, static/private storage, backups/restores and monitoring; map service capacity/usage review.
- [ ] Review Git ignores for virtual environments, private uploads and caches before sharing. Architecture/checklist remain locally ignored and require intentional handoff.

## 24. Account information proposal and Back Office validation batch — 17 October 2026

- [x] Implemented structured 5-category proposals in `apps/accounts/information`: Address, Email, Phone, Contact Person, and Payment Arrangement.
- [x] Domain models: `AccountInformationProposal` and `CustomerContact` defined in `apps/accounts/information/models.py`.
- [x] Migration `apps/accounts/migrations/0006_account_information.py` generated and applied.
- [x] Zero overwrite of authoritative customer data upon submission; snapshots preserved; idempotency enforced via UUID `event_id` and SHA-256 `payload_fingerprint`.
- [x] Back Office validation queue implemented at `/back-office/information-proposals/` with client-scoping, category/status filters, search, and pagination.
- [x] Dedicated permission `accounts.validate_account_information` added; independent four-eye review enforced (`submitted_by != reviewed_by`); mandatory rejection reasons.
- [x] Approval effects: creates verified `CustomerAddress` without altering active assignment frozen coordinates; creates active `CustomerContact` with deduplication; payment arrangement retained on proposal without unauthorized financial side-effects.
- [x] Collector form and entry points: responsive card integrated in `templates/collector/account_detail.html` via `information/includes/card.html` and dedicated form at `templates/information/collector_form.html`.
- [x] Test suite: 70 automated unit tests executed and passed (`manage.py test apps.accounts.information.tests`), 0 errors, 0 failures.

## 25. Current verified reconciliation — 18 October 2026

This section establishes authoritative current project status over prior dated entries where historical narratives conflict.

### Verified database and migration ledger
- **Server versions**: runtime SQL inspection on `ficol_db` confirms **PostgreSQL 18.6** (`PostgreSQL 18.6, compiled by Visual C++ build 1944, 64-bit`) and **PostGIS 3.6.2** (`POSTGIS="3.6.2" [EXTENSION] PGSQL="180" GEOS="3.14.0-CAPI-1.20.0" PROJ="8.2.1"`).
- **Migration ledger**: Migration `collection: 0005` is owner-reported applied in PostgreSQL `ficol_db` and not independently rechecked in this session. Migrations `collection: 0006_verified_points.py` and `accounts: 0007_accountstatuschange_alter_account_options_and_more.py` are generated in code but unapplied to `ficol_db` (pending database application).
- **Physical database tables**: verified that all 23 domain model tables exist in the target database. Framework models and intermediary join tables are present.
- **System checks**: `python manage.py check` passes with zero issues (0 silenced).

### Model inventory reconciliation
- **27 domain models implemented and migrated / owner-reported applied**: User, CollectorProfile, ClientAccess, Client, Customer, CustomerAddress, Account, CustomerContact, AccountInformationProposal, CollectorLocation, Territory, AccountOffer, AccountAssignment, AssignmentReleaseRequest, FieldActivity, FollowUpTask, Document, CommissionRule, CommissionTier, PromiseToPay, Payment, Notification, AuditEvent, plus `InformationEarningRule`, `Commission`, `Payout`, and `PayoutItem` (migration `0005` owner-reported applied in `ficol_db` and not independently rechecked in this session).
- **3 domain models implemented in code with migration unapplied**: `PointRule`, `PointTransaction` (migration `apps/collection/migrations/0006_verified_points.py`), and `AccountStatusChange` (migration `apps/accounts/migrations/0007_accountstatuschange_alter_account_options_and_more.py`). Total domain models in code: 30.
- **2 models from the original proposal remain unimplemented**: The remaining unimplemented original-proposal models are only `CollectorServiceArea` and `IntegrationEvent`.
- **Framework / join tables**: 7 Django framework/GIS models and 3 M2M join tables are distinguished from domain entities.

### Test execution status and attribution
- **Historical database-free unit selection**: 50 tests previously passed across location validation, pure offers, rolling allowance, release requests, document access, and payment validation.
- **Account information unit tests**: 70 unit tests executed and passed (`apps.accounts.information.tests`) during feature development.
- **Known full-suite test baseline**: an earlier test run of the full suite evaluated 294 tests, recording 2 failures and 8 errors. Features affected by these failures and errors cannot be considered fully verified until resolved.
- **Checklist completion standard preserved**: model definitions and table existence do not constitute complete operational or constraint verification; main checkboxes remain unchecked pending resolution of test failures and concurrency suites.

### Workflow and routing status
- **Manual Back Office assignment/reassignment**: implemented in `apps/assignments/services.py`, migration `0005` applied, `assignments.manage_account_assignments` permission active, and `superseded` release status enforced. Concurrency and live workflow tests remain pending.
- **Account-information proposals and validation**: implemented in `apps/accounts/information/`, migration `0006` applied, 5 categories supported, `accounts.validate_account_information` active. Committed on local `main` and on remote tracking branch `origin/feature/pito-information-validation` (based on locally recorded refs); merging to `origin/main` remains pending.
- **Login routing**: direct portal routing via `portal_url(user)` based on Back Office permissions and approved collector profile. `/login/success/` has been removed.
- **Collector dashboard**: live query of active assignments connected, with working "Open account" links and Philippine-time deadlines.

### Incomplete work and unresolved policy register
- Points system: Implemented as verified points awarded upon Back Office approval of submitted information proposals when an active PointRule exists (`PointRule`, `PointTransaction` in `apps/collection/models.py`); migration `0006_verified_points.py` generated (enforces `pointrule_exactly_one_point`); application to database pending. `award_points_for_proposal` enforces approval-state guard (returns no points for pending or rejected proposals). Leaderboards, star ranks, star thresholds, daily caps, manual adjustments, reversals, notifications, external integrations, and broader repeat-information policies remain unimplemented.
- Collector remuneration (`InformationEarningRule`, `Commission`, `Payout`, `PayoutItem`): implemented in code with migration `apps/collection/migrations/0005_information_earnings_and_payouts.py` owner-reported applied to `ficol_db` (not independently rechecked in this session); live operational testing remains pending.
- WIRED CRM integration (`IntegrationEvent`) remains deferred; no external synchronization or API calls.
- Automatic 24-hour inactivity reassignment remains unbuilt; reset triggers, future appointment handling, grace periods, and in-progress exceptions require policy confirmation.
- Account lifecycle transitions are implemented in code, including Back Office authorization, client scoping, idempotency, audit history, collector notification, and terminal/paused work cleanup. The migration `accounts: 0007` is pending application and workflow testing. `resolved` and `closed` are blocked until no payment report remains unverified and `Account.balance_snapshot` is exactly `0.00`; that field remains the stored source snapshot, so live WIRED CRM reconciliation is still unbuilt.
- Offline synchronization and offline field submissions remain unimplemented; the PWA foundation provides an installable application shell and generic offline fallback only.
- Approved rules preserved: multiple owned accounts, exactly one ongoing visit per collector, exactly ten new offer acceptances per rolling 24 hours, non-custodial payment reporting with zero balance/commission side effects.

## 26. Progressive Web App (PWA) foundation batch — 18 October 2026

- [x] Implemented installable Chrome PWA foundation for development over local and HTTPS ngrok environments.
- [x] Root public endpoints registered in `config/urls.py` and handled by `apps/core/views.py`:
  - `/manifest.webmanifest`: served with `application/manifest+json` and `no-cache` headers.
  - `/service-worker.js`: served with `application/javascript`, `no-cache`, and `Service-Worker-Allowed: /`.
  - `/offline/`: branded generic offline fallback page, served with zero business/account/customer data.
- [x] Web app manifest (`templates/pwa/manifest.webmanifest`):
  - Standalone display mode, `start_url: /login/`, `scope: /`.
  - Themed with FiCol blue palette (`#1D4ED8` primary, `#0B192C` background).
  - 192×192 and 512×512 PNG icons generated locally from `static/img/favicon.svg` without external downloads.
- [x] Service worker (`templates/pwa/service-worker.js`):
  - Root scope registration with cache name `ficol-pwa-v1`.
  - Precaches only `/offline/` and safe static icon assets.
  - Network-first navigation fallback to `/offline/` on network failure; dynamic HTML pages are never cached.
  - Network-first with cache fallback for same-origin GET `/static/` assets only.
  - Strict data safety: never intercepts or caches mutations (POST, PUT, PATCH, DELETE) or dynamic data (JSON/APIs, authenticated HTML, accounts, notifications, private media, evidence, payments).
  - Controlled lifecycle: handles `SKIP_WAITING` on user request; never forces automatic reload during active form entry.
- [x] Application shell integration (`templates/base.html`, `static/js/pwa/pwa-register.js`, `static/css/components/pwa.css`):
  - Injected manifest link, theme metadata, PWA stylesheet, and registration script.
  - Accessible `#pwa-update-notice` banner initially hidden, revealed only when a new worker is waiting.
- [x] Scope boundary maintained:
  - This is an installable PWA shell and offline fallback only.
  - Offline account data caching, offline field submissions, background synchronization, and push notifications remain completely unimplemented.
- [x] Automated test coverage: 6 focused PWA tests in `apps/core/tests.py` covering routes, MIME types, cache headers, manifest structure, icon dimensions, worker safety rules, and offline fallback.

## 27. Remote Contact Attempt Logging Batch — 18 October 2026

- [x] Implemented remote Contact Attempt logging (Call, SMS, Email) on active account assignments.
- [x] Records an internal history entry only; no automatic call, SMS, email, or external message is sent.
- [x] Data model updates:
  - Added `sms` to `FieldActivity.kind` choices (`[("visit", "Visit"), ("phone", "Phone"), ("sms", "SMS"), ("email", "Email"), ("skip_trace", "Skip trace")]`) and updated `activity_valid_kind` CheckConstraint.
  - UI labels `phone` as `Call`.
  - Contact attempts save with: `status="submitted"`, `review_status="not_required"`, server-generated `occurred_at` and `ended_at`, `notes`, `outcome`, `submission_fingerprint`.
  - GPS/location fields are omitted (`position`, `accuracy_m`, `distance_m`, `location_captured_at` are null; `location_issue_reason=""`).
  - No promises, customer payments, follow-up tasks, visit scheduling, assignment changes, points, commission, or account-status changes are triggered.
- [x] Migration and testing status:
  - Remote Call/SMS/Email logging code and its Django-generated migration (`apps/activities/migrations/0008_fieldactivity_add_sms_kind.py`) are complete.
  - Migration `activities.0008_fieldactivity_add_sms_kind` is applied in the local development database (`ficol_db`). No production deployment status is claimed.
  - Physical device/browser workflow testing remains pending.
- [x] Protected endpoint and service:
  - Route: `POST /collector/accounts/<assignment_id>/contact-attempts/` (`activities:record_contact_attempt`).
  - Strict payload validation: `event_id` (UUID), `kind` (`phone`, `sms`, `email`), `outcome` (`contacted`, `unavailable`, `disputed`, `other`), `notes` (non-blank trimmed text, max 5,000 characters). Extra keys, invalid types, invalid UUIDs, blank notes, unsupported methods, and unsupported outcomes rejected with HTTP 400.
  - Authorization: Authenticated collector with active assignment and active client access required. Denies unauthenticated (401), Back Office users (403), other collectors (404), inactive assignments (404), and inactive clients (404).
  - Concurrency & retry safety: Exact retries with same `event_id` and identical normalized data return existing activity without duplicate creation. Reusing `event_id` with different details returns HTTP 409 conflict.
  - Audit logging: Statically declared and owned in `apps/audit/services.py` (`ALLOWED_CHANGE_FIELDS` entry `"contact_attempt_recorded": {"status", "kind", "outcome"}`); appends `contact_attempt_recorded` audit event with the collector as actor and the `FieldActivity` as entity.
  - Automatic notification creation is not added.
- [x] Collector account detail integration:
  - Added Contact attempt section directly above Collection visit in `templates/collector/account_detail.html`.
  - Form includes Method (Call, SMS, Email), Outcome (Customer contacted, Customer unavailable, Customer disputed the account, Other), Notes, and Save button.
  - Client-side JS generates one UUID per pending submission, disables button during flight, submits JSON safely with CSRF protection, and reloads on confirmation.
  - Scoped recent history renders the latest 20 contact attempts for the current collector, assignment, and account in Philippine time, completely separated from visit history.
- [x] Unimplemented scope boundaries maintained:
  - Automatic external messaging, contact-attempt review, points, follow-up creation, and automatic reassignment remain unimplemented.

## 28. Dynamic Client-Rate, Commission, and Payout Batch — 19 October 2026

- [x] Implemented dynamic client information earning rates, immutable commission snapshotting upon proposal approval, and company payout batching.
- [x] Data models implemented in `apps/collection/models.py`:
  - `InformationEarningRule`: Client-specific, versioned, positive fixed PHP amount per category (`address`, `email`, `phone`, `contact_person`, `payment_arrangement`). Unique active rule per client and category enforced by database UniqueConstraint (`["client", "category"]` where `is_active=True`). UniqueConstraint on `["client", "category", "version"]`. CheckConstraints enforce positive amount, positive version, PHP currency, and retirement metadata consistency.
  - `Commission`: Immutable snapshot linked OneToOne to approved `AccountInformationProposal`. Captures client, collector (`CollectorProfile`), earning rule reference, snapshot fields (`category`, `amount`, `currency="PHP"`, `rule_version`), review lifecycle fields (`reviewed_by`, `reviewed_at`, `rejection_reason`), and settlement state (`pending`, `approved`, `rejected`, `paid`). CheckConstraints enforce positive amount, PHP currency, valid status, valid category, and review evidence consistency.
  - `Payout`: Company disbursement batch grouping approved commissions for one client and collector. Lifecycle state: `draft`, `approved`, `paid`. Records `client`, `collector`, `currency="PHP"`, `amount`, `reference`, `created_by`, `approved_by`, `approved_at`, `paid_by`, `paid_at`. CheckConstraints enforce nonnegative amount, PHP currency, valid status, and lifecycle evidence pairing.
  - `PayoutItem`: Links a `Payout` to a settled `Commission` with a OneToOne constraint ensuring each commission is included in at most one payout.
- [x] Back Office permissions added to `apps/user/models.py` `BACK_OFFICE_PERMISSIONS`:
  - `collection.manage_earning_rules`
  - `collection.review_commissions`
  - `collection.manage_payouts`
- [x] Service layer and proposal integration:
  - `publish_earning_rule`: acquires client row lock inside transaction to prevent first-time publication race conditions; retires preceding active version for the same client and category; creates new incremented version.
  - `create_commission_for_proposal`: automatically invoked in `apps/accounts/information/services.py` upon proposal approval if an active rule exists; if no active rule exists, approval succeeds without commission; rejections produce no commission.
  - `review_commission`: allows Back Office reviewers to approve or reject pending commissions; rejection requires non-empty reason; self-commission review is allowed for authorized Back Office reviewers with collector profiles.
  - `create_payout`: groups approved unpaid commissions for a client/collector pair into a draft payout.
  - `approve_payout`: advances draft payout to approved.
  - `mark_payout_paid`: requires disbursement reference string; atomically sets payout and all linked commissions to `paid`.
  - `collector_earnings_summary`: aggregates collector amounts across stages (`awaiting_approval`, `approved_unpaid`, `paid_out`, `total`) to avoid double-counting.
- [x] Audit logging in `apps/audit/services.py`:
  - Statically declared in `ALLOWED_CHANGE_FIELDS`:
    - `earning_rule_published`: `{"category", "version", "amount"}`
    - `commission_created`: `{"category", "amount", "status"}`
    - `commission_approved`: `{"status"}`
    - `commission_rejected`: `{"status", "reason"}`
    - `payout_created`: `{"status", "item_count", "amount"}`
    - `payout_approved`: `{"status"}`
    - `payout_paid`: `{"status", "reference"}`
- [x] Web pages, routes, navigation, and dashboard:
  - URLs: `/backoffice/earning-rules/`, `/backoffice/commissions/`, `/backoffice/commissions/<id>/review/`, `/backoffice/payouts/`, `/backoffice/payouts/create/`, `/backoffice/payouts/<id>/approve/`, `/backoffice/payouts/<id>/pay/`, `/collector/commissions/`.
  - Templates populated: `templates/backoffice/earning_rules.html`, `templates/backoffice/commissions.html`, `templates/backoffice/payouts.html`, `templates/collector/commissions.html`.
  - Back Office sidebar updated with Finance navigation group (`Earning Rates`, `Commissions`, `Payouts`).
  - Supervisor dashboard updated with stat cards for pending commissions and draft payouts.
  - Collector sidebar Commissions link activated.
  - Collector dashboard commissions section connected to live `collector_earnings_summary` data.
- [x] Migration and verification status:
  - Django migration generated via `makemigrations collection --name information_earnings_and_payouts`: `apps/collection/migrations/0005_information_earnings_and_payouts.py`.
  - Migration `0005` is owner-reported applied to the local development database (`ficol_db`) and not independently rechecked in this session.
  - Physical device/browser workflow testing remains pending.
- [x] Scope boundaries maintained:
  - Remuneration applies strictly to validated information categories (`address`, `email`, `phone`, `contact_person`, `payment_arrangement`).
  - Does NOT alter or repurpose `CommissionRule` / `CommissionTier` used for account offers and payment terms.
  - No customer fund custody, no payment gateway integration, and no account balance modifications.

## 29. Stars & Performance (Verified Activity Points) Batch — 19 October 2026

- [x] Implemented approved "Stars & Performance" feature as verified points for validated customer information.
- [x] Data models implemented in `apps/collection/models.py`:
  - `PointRule`: Global Back Office configuration. Versioned, positive points (exactly 1 whole point in current version) per approved category (`address`, `email`, `phone`, `contact_person`, `payment_arrangement`). One active rule per category enforced by database UniqueConstraint (`pointrule_unique_active_category` on `["category"]` where `is_active=True`). UniqueConstraint `pointrule_unique_category_version` on `["category", "version"]`. CheckConstraints enforce exactly one point (`points=1`, named `pointrule_exactly_one_point`), positive version (`version__gt=0`), valid category, and retirement consistency (`is_active=True` without retirement metadata, or `is_active=False` with `retired_at` and `retired_by`).
  - `PointTransaction`: Append-only awarded points record. OneToOne relation to `AccountInformationProposal` for duplicate protection. Captures `collector` (`CollectorProfile`), `point_rule` (`PointRule`), `category`, `points` (1 pt snapshot), `rule_version`, `approved_by` (`User`), safe non-sensitive `event_key` (derived from proposal PK and category), and `created_at`. CheckConstraints enforce positive points, positive rule version, and valid category.
- [x] Back Office permissions in `apps/user/models.py`:
  - Added `collection.manage_point_rules` to `BACK_OFFICE_PERMISSIONS`.
- [x] Service layer and proposal integration:
  - `publish_point_rule`: retires preceding active version for the category; increments version and creates new active rule version; logs audit event.
  - `award_points_for_proposal`: automatically invoked in `apps/accounts/information/services.py` inside `review_proposal` database transaction upon approval; approval-state protection ensures points are only awarded if `proposal.status == AccountInformationProposal.Status.APPROVED` (pending and rejected proposals return no transaction); if an active point rule exists for `proposal.category`, creates the `PointTransaction`; if no active rule exists, approval succeeds with no points awarded; idempotent duplicate-prevention enforced.
  - `collector_points_summary`: aggregates verified points and returns breakdown for all 5 categories (including zero).
  - `collector_point_transactions_list`: queries collector point transactions in reverse chronological order.
  - `reviewable_point_rules`: queries point rules for authorized Back Office staff.
- [x] Audit logging in `apps/audit/services.py`:
  - Statically declared in `ALLOWED_CHANGE_FIELDS`:
    - `point_rule_published`: `{"category", "version", "points"}`
    - `point_awarded`: `{"category", "points"}`
- [x] Web pages, routes, navigation, and dashboard:
  - Collector Stars & Performance page: `/collector/performance/` using `templates/collector/performance.html`, showing total points hero card, 5-category breakdown cards (including zero), and recent history table.
  - Back Office point rules management: `/backoffice/point-rules/` using `templates/backoffice/point_rules.html`, added under Finance in Back Office sidebar.
  - Collector sidebar "Stars & Performance" navigation link activated.
  - Collector dashboard Section 7 connected with live verified points and direct link to `/collector/performance/`.
- [x] Migration and verification status:
  - Django migration generated via `makemigrations collection --name verified_points`: `apps/collection/migrations/0006_verified_points.py` (enforcing `pointrule_exactly_one_point`).
  - Migration `0006` is NOT applied to the local development database (`ficol_db`); applying migration 0006 remains pending.
  - Unit tests covering model validation, service permissions, point awarding, idempotency, dashboard integration, and template rendering pass.
  - Local database-backed tests remain unexecuted due to PostgreSQL test database runner permissions (`permission denied to create database`).
- [x] Scope boundaries maintained:
  - Points are implemented strictly as verified information points.
  - Leaderboards, star ranks, star thresholds, daily caps, manual adjustments, reversals, notifications, external integrations, and broader repeat-information policies remain unimplemented.
  - Points never affect commissions, payouts, customer account balances, payment status, or account status.

## 30. System Admin Portal Batch — 20 October 2026

- [x] Implemented approved superuser-only System Admin portal:
  - Base routes registered under `/system-admin/`: dashboard, users list, user create, user edit, clients list, client create, client edit, and audit log.
  - Portal uses existing models: `User`, `CollectorProfile`, `Client`, `ClientAccess`, `AuditEvent`, and Django framework `Permission`.
  - Zero model changes or migrations required; `python manage.py makemigrations --check --dry-run` reports "No changes detected".
- [x] Authorization and portal separation:
  - Reusable `system_admin_required` decorator in `apps/user/admin_portal/permissions.py` enforces active superuser authorization (`is_superuser=True` and `is_active=True`).
  - `is_staff` does not grant access. Anonymous requests redirect to login via Django's `redirect_to_login(request.get_full_path())`, safely preserving query strings without splitting or overwriting login query parameters. Authenticated non-superusers receive 403 Forbidden.
  - Route guards: read-only views (`system_admin_dashboard`, `system_admin_users`, `system_admin_clients`, `system_admin_audit`) are guarded with `@require_GET`. Write views (`system_admin_user_create`, `system_admin_user_edit`, `system_admin_client_create`, `system_admin_client_edit`) are guarded with `@require_http_methods(["GET", "POST"])` and explicit `@csrf_protect`. Unsupported methods return HTTP 405 Method Not Allowed.
  - `portal_url()` in `apps/user/views.py` routes active superusers to `system_admin_dashboard` before Back Office precedence. Ordinary approved collectors and Back Office users retain their respective routing.
  - Back Office sidebar includes a "System Admin" link only when `request.user.is_superuser`.
  - System Admin sidebar includes a "Back Office" navigation link for superusers.
- [x] User and Access management:
  - User creation creates ordinary non-superuser accounts; username and matching temporary passwords required; email, first name, and last name optional; optional pending `CollectorProfile` creation. No emails or notifications sent.
  - User edit manages `is_active`, active `ClientAccess` scopes, direct Back Office capabilities, collector approval status, and optional password resets.
  - Password validation: User creation (`UserCreateForm`) and password reset (`UserEditForm`) enforce Django's configured password validators (`django.contrib.auth.password_validation.validate_password`), rejecting weak passwords and attaching validation errors directly to `password` or `new_password` fields.
  - Superuser accounts are strictly read-only system accounts in user management; editing, deactivating, password-resetting, or access-changing is prevented.
  - Direct Back Office capabilities manage only existing database `Permission` records matching `apps.user.models.BACK_OFFICE_PERMISSIONS`. Unrelated direct permissions and all group memberships are preserved.
  - Portal Precedence Warning: prominent warning explains that granting Back Office capabilities to a collector routes them to Back Office and prevents Collector page access.
  - `ClientAccess` soft-deactivation and reactivation via `is_active`; historical access records retained without deletion. Inactive clients cannot receive active access.
  - Collector approval sets `approved_by` to the acting superuser and `approved_at` to the current time; suspending or rejecting preserves prior approval evidence.
- [x] Client management:
  - List, search, pagination, create, and edit for `code`, `name`, and `is_active`.
  - Calls `client.full_clean()` before saving; GPS and policy settings are unexposed and unaltered.
- [x] Administrative audit log:
  - Six allowlisted administrative actions declared in `apps/audit/services.py`:
    - `system_admin_user_created`: `{"is_active", "collector_profile_created"}`
    - `system_admin_user_updated`: `{"is_active", "backoffice_permission_count", "active_client_access_count", "collector_profile_created"}` (adding a pending Collector Profile to an existing user is audited through this event recording `collector_profile_created: True`)
    - `system_admin_password_reset`: `{"password_reset"}` (evidence strictly `{"password_reset": true}`)
    - `system_admin_collector_status_changed`: `{"approval_status"}`
    - `system_admin_client_created`: `{"code", "is_active"}`
    - `system_admin_client_updated`: `{"code", "is_active", "name_changed"}` (records boolean `name_changed: True` when name changes alone or with code/active status, never logging raw client name string to preserve 100-character audit text limits; emits no audit if no business field changed)
  - All audit payloads conform to the audit event schema and stay well within the 100-character audit text limit.
  - Audit page is read-only, paginated, and filterable by action and actor.
- [x] Templates & styling:
  - Plain server-rendered Django templates and POST-redirect-GET pattern in `templates/system_admin/`.
  - Checkbox selection preservation: bound invalid user-edit form submissions preserve selected client-access and Back Office capability checkboxes using form field values, while continuing to render initial selections on unbound GET requests.
  - No client-side JavaScript, AJAX, charts, or external CDNs added.
  - Branded stylesheet `static/css/system_admin/portal.css` using FiCol visual language (dark navy text, FiCol blue actions, white cards, min 44px touch targets).
- [x] Verification:
  - 59 database-free unit and template rendering tests passed (30 in `apps.user.admin_portal.tests`, 16 in `apps.user.tests`, 13 in `apps.audit.tests`).
  - `python manage.py check` passed with zero issues.
  - `python manage.py makemigrations --check --dry-run` reports "No changes detected".
  - Full-suite database-backed tests remain unexecuted due to local PostgreSQL test-database permissions (`permission denied to create database`).

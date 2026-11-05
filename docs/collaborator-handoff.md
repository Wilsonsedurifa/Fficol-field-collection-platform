# FiCol collaborator handoff

Prepared for Raf's collaborating developer and their coding agent.

Baseline: **18 October 2026**. Read this file and the checked-out code before planning changes. This is a scoped development brief, not blanket permission to change files, data, permissions, or deployment settings.

## 1. Historical assignment and current guidance

### 1.1 Historical development brief (completed)

The initial assignment for Raf's collaborating developer was to build the **collector account-information proposal and Back Office validation workflow**.

> [!NOTE] Implementation status (18 October 2026)
> This feature was implemented in `apps/accounts/information` across all five categories (Address, Email, Phone, Contact Person, Payment Arrangement) with migration `accounts: 0006_account_information` applied in the local target database `ficol_db`. The implementation includes `CustomerContact` (11 concrete fields) and `AccountInformationProposal` (20 concrete fields), along with the Back Office validation queue with permission `accounts.validate_account_information`. The code is committed on local `main` and on remote tracking branch `origin/feature/pito-information-validation` (based on locally recorded refs); merging into `origin/main` remains pending.

The five categories supplied by the business were:
1. Address.
2. Email.
3. Phone number.
4. Contact person.
5. Payment arrangement.

Points and compensation remain outside this feature and belong to separate future work.

### 1.2 Current guidance for collaborators

1. **Verification and test execution:** Do not treat previous test passes as fully verifying all features. An earlier full test suite run evaluated 294 tests, recording 2 failures and 8 errors; affected features remain unverified until resolved on a disposable PostGIS test database.
2. **Branch integration:** Verify and test the branch integration between `feature/pito-information-validation` and `origin/main` against a disposable test database before merging.
3. **Preserve business boundaries:** Preserve approved rules (multiple assignments per collector, exactly one ongoing visit per collector, and up to ten new offer acceptances per rolling 24 hours). Do not invent production policies for unbuilt modules (points ledger, remuneration/payouts, WIRED integration, automatic inactivity reassignment, or financial closure/reconciliation).

## 2. What FiCol is

FiCol is a real field-collection website built with Django. Collectors receive nearby offers, accept accounts, visit customers, report outcomes, request follow-ups, and upload evidence of customer payments. Back Office reviews evidence and controls operational decisions.

An account here means a collection case, not a login identity. Both portals share collection accounts and use server-side role, client-scope, and ownership authorization.

The product has a light slate/white interface, indigo actions, restrained status colors, rounded panels, and responsive sidebar/navbar components. Preserve the existing visual language and usability on phones and outdoors.

## 3. Confirmed business requirements and boundaries

The original product brief and answered process questionnaire established:

- Collector registration/approval is administered by the company. Local approval exists for development; CRM integration is deferred.
- Collectors can own multiple accounts, with only one ongoing visit across accounts.
- Raf approved a limit of ten new offer acceptances per rolling 24 hours. Release/completion does not refund an acceptance. Active workload is a separate count.
- Collection accounts have one exclusive active owner until an authorized ownership transition.
- Follow-up requests need staff scheduling/authorization. Visit review is a separate decision.
- Collector-requested account release needs administrative approval.
- Collectors cannot receive customer funds. They may assist the customer and upload proof of payment to authorized channels.
- Collector remuneration is company-funded, client-specific, and tied to validated information; actual rates and payout rules remain unresolved.
- Valid address, email, phone, contact person, and payment arrangement are each specified as **one point**, subject to administrative validation. Deduplication, repeat submissions, caps, and reversals are unresolved. Do not award points yet.
- WIRED integration is deferred. Do not add APIs, imports, synchronization, or external communications in this batch.

The source documents are not committed with this handoff. Ask Raf for the relevant approved excerpts if a business ambiguity blocks a design decision. Do not invent answers or assume that a configurable value has been approved as company policy.

## 4. Current implementation baseline

Existing code includes:

- Shared login, ordinary collector portal, separate Back Office portal, and explicit client grants.
- Direct portal login routing based on role/permissions; `/login/success/` has been removed.
- Validated GPS saving, online/offline availability, Leaflet/OpenStreetMap offers, acceptance, My Accounts, and assigned-account detail.
- Collector dashboard displays live assigned accounts with working account links; performance and reward summaries remain placeholders.
- Visits, promises, report history, one-ongoing-visit enforcement, and Back Office visit review.
- Collector follow-up requests and Back Office earliest-start/completion-deadline scheduling.
- Private document upload/access, payment reporting, and independent payment verification.
- Collector release requests with requested, approved, rejected, and superseded states, reviewer scope, task cancellation and preserved acceptance counts.
- Manual Back Office assignment/reassignment (`apps/assignments/services.py`, migration `assignments: 0005`, commit `f252bec`), with `AccountAssignment.event_id`, dedicated `assignments.manage_account_assignments` permission, and `superseded` release status.
- Collector account-information proposals and Back Office validation (`apps/accounts/information/`, migration `accounts: 0006`), covering all 5 categories, with dedicated permission `accounts.validate_account_information`.
- Local compiled Tailwind, responsive shared navigation, and separate scripts/styles by page/component.

Raf manually tested location/offer acceptance, visits, and release request -> rejection -> resubmission -> approval. Release approval removed active ownership without refunding the acceptance. These tests do not prove all database constraints, concurrency, file-access paths, or production readiness.

**Planned / deferred in this baseline:** automatic inactivity/missed-task transfer, points/commission awards/payouts, operational closure/reconciliation, offline synchronization, and WIRED integration.

Raf's parallel workstream for manual assignment/reassignment landed in commit `f252bec` with migration `assignments: 0005` applied, introducing `AccountAssignment.event_id`, `manage_account_assignments` permission, and `superseded` release status.

## 5. Code map and first reading list

Read root `README.md` for the complete local setup and operational overview, then inspect:

| Location | Why it matters |
| --- | --- |
| `apps/user/models.py` | User, CollectorProfile, ClientAccess, and BACK_OFFICE_PERMISSIONS (6 permissions) |
| `apps/user/views.py` and `urls.py` | Direct portal login routing and Back Office page integration |
| `apps/accounts/models.py` | Client, Customer, CustomerAddress, Account |
| `apps/accounts/information/` | Models (CustomerContact, AccountInformationProposal), services, views, and urls for 5-category proposals and validation |
| `apps/assignments/models.py` and `services.py` | Ownership, profile/client guards, frozen assignment snapshots, manual assignment/reassignment, lock/retry conventions |
| `apps/activities/models.py` and `services.py` | Visits, follow-ups, document metadata, scoped review patterns |
| `apps/activities/views.py` | Shared Back Office permission/context helper |
| `apps/collection/services.py` | Private document access and payment verification boundaries |
| `apps/audit/models.py` and `services.py` | Bounded allowlisted audit evidence |
| `templates/collector/account_detail.html` | Collector entry point with embedded information proposal card |
| `templates/backoffice/base.html`, `components/` | Existing portal shell and navigation |
| `static/css/backoffice/portal.css` | Shared local styling; coordinate edits |
| `frontend/package.json` and `tailwind.config.js` | CSS build/scanning configuration |

There are 23 domain model definitions in the current baseline (including `CustomerContact` and `AccountInformationProposal`), distinct from 7 Django framework/GIS models and 3 framework intermediary M2M join tables. Seven models from the original proposal remain unimplemented (`CollectorServiceArea`, `Commission`, `Payout`, `PayoutItem`, `PointRule`, `PointTransaction`, `IntegrationEvent`).

The detailed local documents are `docs/database-architecture-proposal.md` and `docs/database-implementation-checklist.md`. **They are ignored by Git**, so your clone may not contain them. Raf must share an approved copy separately. Missing local architecture files do not authorize reconstructing their contents from assumptions.

## 6. Proposed feature behavior to plan

### Collector submission

- Provide a clear account-detail entry point for proposing information in the five categories.
- Require an active approved ordinary collector, current active ownership, active client scope, and an eligible account. Recheck on POST; hiding a button is not security.
- Collect category-appropriate structured values, explanation, and evidence/reference where required by the approved policy. Display plain labels and validation errors.
- Preserve a snapshot or stable reference to the current value and the proposed value. Keep provenance attached to the account/customer/client and the submitting assignment/user.
- Do not overwrite authoritative customer data merely because a collector submitted a proposal.
- Use UUID idempotency plus exact payload matching. Identical retries must not create duplicate proposals or audit rows; changed payloads under the same token must conflict.
- Show the allowed status/history to the collector without revealing other accounts' or former-owner private data.

### Back Office validation

- Provide a client-scoped queue with category/status filters, search, pagination, current value, proposed value, explanation, evidence access, and provenance.
- Propose a dedicated validation permission and integrate it into portal classification/context/navigation. A validation-only staff user must be able to enter Back Office without gaining finance/visit/assignment powers.
- Require an active reviewer and active client access, and prohibit reviewing their own submission.
- Approve or reject with recorded reviewer/time. Rejection requires a reason. Final decisions must not be silently overwritten by another reviewer or retry.
- Define, before implementation, which approved fields can update authoritative data and which are recorded as validated claims. Address changes must not rewrite an existing assignment's frozen target coordinates or invalidate historical evidence.
- Payment-arrangement validation must not silently fulfill a promise, verify a payment, alter balance, close an account, or create a new payable entitlement.
- Leave points and compensation untouched. A validated proposal provides a future reward source; it does not award a point by itself in this batch.

### History and access

- Preserve previous values, proposals, decisions, and attribution. Corrections should create explicit history rather than deleting submitted evidence.
- Former collectors must lose current account/private-document access after release or reassignment. Original uploader identity must not bypass current access rules.
- Decide how a pending proposal is reviewed after ownership ends. Preserve history without granting the former owner access; ask Raf to approve the review policy.
- Use transactional validation and audit recording with the project's bounded-retry conventions. Review actual lock order against concurrent ownership, review, and address operations; do not claim universal deadlock freedom.

## 7. Decisions you must surface in your plan

Do not silently default these:

1. Existing authoritative fields versus new CustomerContact/proposal models: avoid duplicate competing sources of truth.
2. Whether phone/email/contact-person proposals can contain multiple values and what counts as a meaningful change.
3. Address requirements: text, coordinates, verifier evidence, and whether a new address becomes the collection address for future assignments only.
4. Payment-arrangement structure, relation to PromiseToPay, and validation effect.
5. Evidence required per category, including non-payment/unavailable-customer cases. Do not require a fabricated payment receipt.
6. Handling equivalent/duplicate proposals, competing approvals, stale current values and approved corrections.
7. Pending proposals after release/reassignment, and the appropriate historical attribution for later rewards.
8. Reversal/correction policy for validated information. Do not invent points reversal accounting.

Recommended plan structure: collector flow, Back Office flow, schema/constraints, permissions, atomic transitions/idempotency, exact files, migration dependencies, tests, manual acceptance checklist, and remaining questions.

## 8. Scope ownership and coordination

| Workstream | Owner |
| --- | --- |
| Assignment/reassignment, ownership transitions, release integration | Raf / Raf's agent |
| Information proposals and Back Office validation | Collaborating developer / their agent |
| Shared schema, migration sequence, routes, permissions, navigation, shared CSS | Coordinate and obtain Raf's approval before edits |
| Points, remuneration, payout, automatic reassignment, WIRED | Deferred or separately authorized |

Likely collision files include `apps/accounts/models.py`, `apps/user/models.py`, shared Back Office context/URLs/navigation, account detail, `portal.css`, audit allowlists, docs, and compiled Tailwind output. State which ones you need before coding. Prefer scoped feature templates/styles/services where appropriate, but do not create a new app or split architecture without agreement.

Do not rename common routes, existing HTML hooks, model fields or scripts to make your feature easier. Do not merge unrelated cleanup into the feature.

## 9. Development setup

Use your own checkout and local development database. Obtain the repository URL and agreed base branch/commit directly from Raf; this file does not assume their names.

From the repository root, using PowerShell:

```powershell
python -m venv ficolenv
.\ficolenv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build:css
python manage.py check
python manage.py showmigrations
python manage.py migrate --plan
```

Create your local `.env` with DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT using your own credentials. Never copy production credentials or private uploads into a shared repository.

Existing development used Python 3.13.7, Django 6.1.2, PostgreSQL 18.6 / PostGIS 3.6.2 (previously noted as PostgreSQL 17 / PostGIS 3.5.3) and Tailwind 3.4.17. Consult the current requirements/lockfile rather than installing arbitrary latest packages. GeoDjango needs compatible GDAL/GEOS native libraries; Windows overrides are GIS_LIBRARY_DIR, GDAL_LIBRARY_PATH and GEOS_LIBRARY_PATH. See README for details.

Apply reviewed migrations only to your authorized local database. Enable PostGIS through an authorized database administrator. Once configured, run `python manage.py runserver` and open `/login/`. Use your own separate collector and reviewer identities with explicit client access; no shared passwords are included here.

## 10. Git and migration workflow

1. Agree on the latest base commit with Raf. A branch cannot contain another developer's uncommitted local files; confirm this handoff, README, requirements and frontend move have been committed/shared.
2. Create a feature branch, for example `feature/account-information-validation`, from that agreed base. Never work directly on Raf's active branch or shared working directory.
3. Implement only the approved batch. Keep commits reviewable and exclude secrets, generated dependency folders, virtual environments, private uploads and customer data. Check Git ignores; do not assume all sensitive/local paths are already covered.
4. Coordinate shared app migration dependencies before generating migrations. Preserve applied migrations; generate a new migration through Django. If both branches create migrations in the same app, reconcile dependencies together rather than overwriting/renumbering applied history.
5. Regenerate compiled CSS when approved template/class changes require it. Share source changes and the resulting artifact; do not manually combine minified CSS.
6. Open a pull request with problem/behavior, schema and file scope, tests actually run, unrun checks, migration/application instructions, and desktop/mobile evidence. No deployment or merge without Raf's approval.

Dependency installs generate node_modules and lock/build outputs; migration/build commands generate files too. List expected outputs in the approval request. Do not add scratch/helper/backup/test-output files without permission.

## 11. Acceptance criteria and tests

Use the existing Django testing conventions. New test files require approval just like application files. Do not test by altering live account records.

- Approved collector can submit a valid proposal only for an actively owned, client-scoped account.
- Anonymous, unapproved, inactive, wrong-role, cross-client and former-owner submissions are denied server-side.
- Authoritative data is unchanged while a proposal is pending or rejected.
- Authorized independent review records a stable result and the approved authoritative-data effect, with complete history.
- Exact retries create no duplicates; altered token payloads and stale/conflicting updates return clear conflicts.
- Competing reviewers cannot both finalize the same proposal or apply incompatible authoritative values.
- Approval does not rewrite frozen assignment locations, verify customer payments, change balances, award rewards or transfer ownership.
- Evidence access remains private and ownership/client-scoped; submitted text is escaped and user-facing messages contain no internal implementation jargon.
- A validation-only reviewer has the proper portal entry/navigation without unintended access to other workflows.
- Forms work with CSRF protection, keyboard navigation and phone layouts; show meaningful errors and empty states.
- Database constraints/persistence/concurrency are tested against a **disposable PostGIS test database**. Use nested savepoints for expected IntegrityErrors and assert the intended constraint where necessary.
- Report actual executed tests separately from mocks and written-but-unrun database/browser tests. Database privilege restrictions are not permission to redirect tests to the live database or grant privileges unilaterally.

Test execution status and historical attribution:
- 70 automated unit tests executed and passed (`manage.py test apps.accounts.information.tests`) during feature development.
- The known full-suite baseline from an earlier test run evaluated 294 tests, recording 2 failures and 8 errors. Features affected by these failures and errors cannot be considered fully verified until resolved.
- Current read-only checks confirmed that `python manage.py check` passes with 0 issues, and `showmigrations` verified all 43 migrations applied in the local database. Explicit test modules such as `python manage.py test apps.accounts.tests`, JavaScript syntax checks, and the frontend build remain reproducible. Use the project's actual test class names; avoid nonexistent labels or counting mocked tests as database verification.

## 12. Ready-to-send first message for your coding agent

> Read docs/collaborator-handoff.md and README.md, then inspect the current models, services, permissions, routes and templates listed in the handoff. Your task is collector account-information proposals and independent Back Office validation for address, email, phone, contact person and payment arrangement. Raf's parallel workstream owns assignment/reassignment; coordinate shared files and migrations. First produce a scoped implementation plan, schema proposal, unresolved decisions, exact existing/new file list, test plan and expected generated outputs. Do not implement, install dependencies, generate migrations, create helper files, change database records, grant permissions, or deploy until Raf approves the specific batch. Do not implement points, compensation, payment processing, automatic reassignment or WIRED integration. Preserve existing ownership/client-scope security and historical data.

## 13. Completion handoff to Raf

Return a pull request plus a short explanation containing:

- What the collector submits and exactly what approval changes.
- Model/constraint/migration additions and dependencies on Raf's branch.
- New permission/client-scope setup needed, without applying grants automatically.
- Actual automated tests and manual desktop/mobile outcomes, with blocked tests clearly identified.
- A reproducible manual test from proposal through rejection, resubmission and approval, plus access denial after ownership ends.
- Remaining questions and integration risks, especially information deduplication, address changes and future reward attribution.

Do not call this feature production-ready until both branches are integrated and cross-workflow security, database concurrency and deployment checks have actually passed.

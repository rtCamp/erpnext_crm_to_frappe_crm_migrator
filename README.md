# ERPNext CRM → Frappe CRM Migrator

One-time migration tool to move data from **ERPNext CRM** (Lead, Opportunity, Prospect, plus the surrounding lookup masters) to **Frappe CRM** (CRM Lead, CRM Deal, CRM Organization, …) on the same site.

## What It Does

Copies records, child tables, and activity references from the ERPNext CRM tables to their Frappe CRM equivalents. Source-meta preservation (`name`, `owner`, `creation`, `modified`, `modified_by`, `docstatus`) keeps the audit trail intact and makes any document that references a migrated record by name continue to resolve.

### Migration pairs

| Source (ERPNext) | Target (Frappe CRM) |
|---|---|
| Lead | CRM Lead |
| Opportunity | CRM Deal |
| Prospect | CRM Organization |
| Territory | CRM Territory |
| Industry Type | CRM Industry |
| UTM Source | CRM Lead Source |
| Opportunity Lost Reason | CRM Lost Reason |
| Item | CRM Product |

### Phases of the migration

1. **Mapping UI + custom-field auto-detection.** Walks each source doctype's meta (custom fields included) and proposes target fields from the registry or by exact-name match. Per-tab review surfaces only fields needing user action; auto-mapped fields are summarised separately.
2. **Core records migration.** Reads the locked `CRM Migration Field Map` and bulk-inserts target rows in dependency order (lookups → parents). Source `name` is preserved on the target so re-runs are idempotent.
3. **Child-table reshape.** Restructures children whose schema changes across the source/target pair:
   - `Opportunity.items` → `CRM Deal.products` (lazy-creates missing CRM Product on the fly).
   - `Opportunity.contact_person` + `Contact.links` → `CRM Deal.contacts` (with primary flag).
   - `Opportunity.lost_reasons` → `CRM Deal.lost_reason` (extras appended to `lost_notes`; lazy-creates missing CRM Lost Reason).
   - `Prospect` contacts → new `Contact.links` rows pointing at CRM Organization (legacy Prospect links kept).
   - `Lead/Opportunity/Prospect.notes` (CRM Note children) → standalone **FCRM Note** documents anchored to the migrated CRM record. Any `custom_note_attachments` Table children on CRM Note are re-anchored to the new FCRM Note.
   - **ToDo handling:**
     - Frappe assignment-style ToDos (description begins with `Assignment for …`) are left in place; only their `reference_type` is flipped to the CRM doctype.
     - Content ToDos are converted 1:1 to **CRM Task** rows with status mapped (Open → Todo, Closed → Done, Cancelled → Canceled; other statuses pass through verbatim).
     - The `_assign` JSON cache on each migrated row is regenerated into ToDo rows so the assignment widget on the CRM doc page resolves correctly.
   - **Customer/Lead bridges for Opportunity.** When an Opportunity references a Customer via `party_name`, a Prospect + CRM Organization are auto-created so `CRM Deal.organization` resolves. Likewise, when an Opportunity is owned by a Lead that belongs to a Prospect, `CRM Deal.organization` is populated alongside `CRM Deal.lead`.
   - **Shared-schema children re-anchor.** Any Table field whose child doctype is identical on source and target (e.g. `status_change_log`, `custom_stage_change_log`) has its child rows re-anchored with one UPDATE per child doctype (no re-insert, child meta preserved).
4. **Activity reference rewrite.** Sweeps audit-trail / activity-bearing doctypes and rewrites their `reference_doctype`-equivalent column from ERPNext source values to Frappe CRM target values. Reference *names* are unchanged thanks to source-meta preservation.

   Doctypes touched: FCRM Note, CRM Task, CRM Call Log, CRM Notification, Comment, Communication, File, Version, Communication Link, Tag Link, View Log, DocShare, Event Participants, Notification Log, CRM Service Level Agreement, and assignment-only ToDos.

The Details tab on the Settings form lists every doctype the activity step touches, its rewritten field, and its scope.

## Requirements

- Frappe v16+
- ERPNext v16+
- Frappe CRM v16+
- All three apps installed on the same site

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/rtCamp/erpnext_crm_to_frappe_crm_migrator --branch develop
bench --site your-site.localhost install-app erpnext_crm_to_frappe_crm_migrator
bench --site your-site.localhost migrate
```

## Usage

1. Open `/app/crm-migration-settings`.
2. **Tip — pre-create matching custom fields on the CRM side** before clicking Refresh Diff. Same-name fields are auto-mapped, so you avoid resolving each one manually.
3. Click **Refresh Diff** to scan source doctypes. Each tab shows:
   - Auto-mapped fields (read-only HTML summary).
   - A table of fields needing your decision: Skip (drop) or Map (with a target field). Use **Mark all as Skip** to bulk-skip an entire tab.
4. Per tab, click **Lock & Freeze** when satisfied. Locking is per-tab — you can iterate on one doctype without affecting others.
5. Per tab, click **Migrate <Source>** to run that step in the background. Watch progress via `/app/crm-migration-run`.
6. When all eight tabs are locked, the top-level **Run Migration** and **Migrate Activity Records** buttons appear. Run Migration runs every step in dependency order (lookups → parents → activity rewrite).
7. **Default: Skip All & Migrate** is the one-click path — refreshes, marks all unmapped fields as Skip, locks every tab, and runs the full migration.

## Architecture

```
erpnext_crm_to_frappe_crm_migrator/
├── api/
│   ├── mapping.py          # refresh_diff, lock_doctype, unlock_doctype,
│   │                       # get_target_doctype_fields, get_migrator_details
│   ├── runner.py           # run_migration, run_activity_only,
│   │                       # _execute_run, _migrate_one_doctype,
│   │                       # _reanchor_shared_children, _preflight_link_check
│   ├── reshape.py          # Phase 3 — Items, Contacts, Lost Reason,
│   │                       # Prospect Contact.links, Notes (FCRM Note),
│   │                       # _assign → ToDo, content ToDo → CRM Task
│   └── activity.py         # Phase 4 — reference rewrite + assignment-ToDo rewrite
├── mapping/
│   └── registry.py         # Source ↔ target field maps (native fields only)
├── _reset_test_data.py     # Dev helper — `reset_all` / `reset_deal_only` to
│                           #   wipe target data and re-run the migration
└── migrator/
    └── doctype/
        ├── crm_migration_settings/    # Single — the user UI
        ├── crm_migration_field_row/   # Child of Settings tabs
        ├── crm_migration_field_map/   # Frozen 4-col runner input
        ├── crm_migration_run/         # Run log header
        └── crm_migration_run_step/    # Per-step counters
```

### Source-meta preservation

The runner uses `frappe.db.bulk_insert(..., ignore_duplicates=True)` and carries the source row's `name`, `owner`, `creation`, `modified`, `modified_by`, `docstatus` directly to the target. This:

- Preserves the audit trail (who created / when, on both sides).
- Makes re-runs idempotent — same primary key + `ignore_duplicates`.
- Lets the activity step rewrite only the `reference_doctype` column on activity rows; `reference_name` stays the same because target name == source name.

The trade-off is naming-series mismatch on doctypes that use a series. For example, an ERPNext `OPP-2024-00001` ends up as `CRM Deal: OPP-2024-00001` instead of `CRM-DEAL-2024-00001`. This is intentional so any external document referencing the source name (Quotations, Sales Orders, custom reports) keeps resolving.

### Bulk insert (no validators / doc events)

The runner deliberately skips `frappe.get_doc().insert()`. Per-row preprocessing builds a values tuple matching a static target-column list, then `bulk_insert` writes a chunk at a time. This:

- Skips per-row validators, hooks, and autoname callbacks.
- Gives 10–100× throughput on large datasets.
- Is acceptable because the target site is fresh and the locked field map is the only validation that matters.

### Lazy dependency creation

The Items and Lost Reasons reshapes auto-create their CRM-side lookup rows on the fly. So even if a user marks Item or Opportunity Lost Reason as fully Skipped, the Opportunity migration still produces correct CRM Deal data — `CRM Product` rows are derived from `tabItem`, `CRM Lost Reason` from `tabOpportunity Lost Reason`.

### Failure handling and diagnostics

- Every per-row, per-chunk, and per-reshape failure writes a full traceback to the **Error Log** with a `Migrator …` title prefix. Filter `/app/error-log` by title to triage a failed run.
- The Run Step row keeps a truncated `last_error` and a comma-separated `sample_failed_names` for an at-a-glance read; the Error Log is the source of truth for the full traceback.
- A step with `failed_count > 0` ends `Failed`; one with `child_failed_count > 0` ends `Failed` too. `Succeeded` requires both to be zero.

### Dev helpers

- `erpnext_crm_to_frappe_crm_migrator._reset_test_data.reset_all` — wipe every CRM target row, revert the activity rewrite, drop the Run history. Preserves the locked CRM Migration Field Map and the per-tab Settings state.
- `erpnext_crm_to_frappe_crm_migrator._reset_test_data.reset_deal_only` — same shape but scoped to the CRM Deal pipeline; use to re-test the Opportunity → CRM Deal step in isolation.
- `erpnext_crm_to_frappe_crm_migrator._debug_opp.run_inline` — execute the Opportunity step synchronously (skips enqueue), then prints the resulting Run record and any Error Log entries from the run window. Same pattern works for any single-source debugging.

```bash
bench --site your-site.localhost execute \
    erpnext_crm_to_frappe_crm_migrator._reset_test_data.reset_deal_only
bench --site your-site.localhost execute \
    erpnext_crm_to_frappe_crm_migrator._debug_opp.run_inline
```

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/erpnext_crm_to_frappe_crm_migrator
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

## License

AGPL-3.0

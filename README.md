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

### Four phases

1. **Mapping UI + custom-field auto-detection.** Walks each source doctype's meta (custom fields included) and proposes target fields from the registry or by exact-name match. Per-tab review surfaces only fields needing user action; auto-mapped fields are summarised separately.
2. **Core records migration runner.** Reads the locked `CRM Migration Field Map` and bulk-inserts target rows in dependency order (lookups → parents). Source `name` is preserved on the target so re-runs are idempotent.
3. **Child-table reshape.** Opportunity Items → CRM Products, Opportunity contact_person + Contact.links → CRM Deal.contacts, Opportunity.lost_reasons → CRM Deal.lost_reason. Lazy-creates missing dependent records (CRM Product, CRM Lost Reason) on demand.
4. **Activity reference rewrite.** Sweeps Comments, ToDos, FCRM Notes, CRM Tasks, CRM Call Logs, CRM Notifications, Communications, and Files; rewrites their `reference_doctype`-equivalent fields from ERPNext source values to CRM target values. Reference names are unchanged.

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

## Architecture

```
erpnext_crm_to_frappe_crm_migrator/
├── api/
│   ├── mapping.py          # refresh_diff, lock_doctype, unlock_doctype,
│   │                       # get_target_doctype_fields
│   ├── runner.py           # run_migration, run_activity_only,
│   │                       # _execute_run, _migrate_one_doctype,
│   │                       # _reanchor_shared_children, _preflight_link_check
│   ├── reshape.py          # Phase 3 — Items, Contacts, Lost Reason,
│   │                       # Prospect Contact.links
│   └── activity.py         # Phase 4 — reference rewrite
├── mapping/
│   └── registry.py         # Source ↔ target field maps (native fields only)
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
- Lets Phase 4 rewrite only the `reference_doctype` field on activity rows; `reference_name` stays the same because target name == source name.

The trade-off is naming-series mismatch on doctypes that use a series. For example, an ERPNext `OPP-2024-00001` ends up as `CRM Deal: OPP-2024-00001` instead of `CRM-DEAL-2024-00001`. This is intentional so any external document referencing the source name (Quotations, Sales Orders, custom reports) keeps resolving.

### Bulk insert (no validators / doc events)

The runner deliberately skips `frappe.get_doc().insert()`. Per-row preprocessing builds a values tuple matching a static target-column list, then `bulk_insert` writes a chunk at a time. This:

- Skips per-row validators, hooks, and autoname callbacks.
- Gives 10–100× throughput on large datasets.
- Is acceptable because the target site is fresh and the locked field map is the only validation that matters.

### Lazy dependency creation (Phase 3)

Items reshape auto-creates `CRM Product` rows from `tabItem` if they aren't already migrated. Lost Reasons reshape auto-creates `CRM Lost Reason` from `tabOpportunity Lost Reason`. So even if a user marks Item or Opportunity Lost Reason as fully Skipped, the Opportunity reshape still produces correct CRM Deal data.

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

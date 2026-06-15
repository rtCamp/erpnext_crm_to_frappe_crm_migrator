# ERPNext CRM → Frappe CRM Migrator

One-time migration tool to move data from **ERPNext CRM** (Lead, Opportunity, Prospect + lookup masters) to **Frappe CRM** (CRM Lead, CRM Deal, CRM Organization, …) on the same site.

![CRM Migration Settings](https://github.com/user-attachments/assets/ba66df7a-b9f2-4699-961c-c8725b869c03)

## What it does

Copies records, child tables, and activity references from ERPNext CRM tables to their Frappe CRM equivalents. Source-meta preservation (`name` / `owner` / `creation` / `modified` / `modified_by` / `docstatus`) keeps the audit trail intact and makes any document that references a migrated record by name keep resolving.

**Migration pairs:**

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

The migration runs in four steps end-to-end — see [docs/architecture.md](docs/architecture.md):

1. **Mapping UI + diff** — propose target fields, user resolves anything ambiguous, locks each tab.
2. **Core records** — bulk-insert target rows in dependency order, preserving source meta.
3. **Schema reshape** — populate Table-typed children + cross-doctype relationships (Items, Contacts, Lost Reasons, Notes, ToDos, Stage Logs, Dynamic Links).
4. **Activity reference rewrite** — flip `reference_doctype` on Comments / ToDos / FCRM Notes / Versions / etc.

Plus a separate **post-migration cleanup** that drops the ERPNext source rows once you've verified the migration — see [docs/dev.md](docs/dev.md).

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Steps, dispatch order, reshape pipeline, source/target pairs |
| [docs/mapping.md](docs/mapping.md) | Field-level mapping: registry, reshape coverage, dynamic-link routing, target customs, value-translation gaps |
| [docs/decisions.md](docs/decisions.md) | Why-X-over-Y log: source-meta preservation, bulk_insert, repoint, name preservation, etc. |
| [docs/dev.md](docs/dev.md) | Settings UI workflow, cleanup button, background-job names, failure triage, dev helpers |

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

## Quick usage

1. Open `/app/crm-migration-settings`.
2. **Pre-create matching custom fields on the CRM side** before clicking Refresh Diff — same-name fields auto-map, no manual resolution needed. (Most are already mirrored by [`frappe_crm_xt`](https://github.com/rtCamp/frappe_crm_xt).)
3. **Refresh Diff** → resolve any unmapped fields (Skip or Map) → **Lock & Freeze** per tab.
4. Click **Run Migration** when all 8 tabs are locked. Or use **Default: Skip All & Migrate** for a one-click cutover.
5. Verify on the Frappe CRM frontend. When happy, click the red **Clean up ERPNext source data** button to drop the originals.

If the migration looks wrong before cleanup runs, the red **Undo migration** button reverts every target-side write (activity refs, reanchored children, dynamic-link repoint, marker-tagged synthetic rows, CRM parent rows). Source data is preserved by the migrator so re-running is non-destructive. See [docs/dev.md](docs/dev.md#undo-migration) for the full phase list.

Watch progress on `/app/crm-migration-run`.

## Screenshots

Each tab shows the per-doctype field mapping — auto-mapped fields (including customs), type-mismatch warnings, and the per-tab **Lock & Freeze** state.

![Lead field mapping](https://github.com/user-attachments/assets/f1cb9b17-4dcb-4b89-b2a2-89de3c7fa49c)

![Lead field mapping (continued)](https://github.com/user-attachments/assets/37dc753a-5e19-4be5-a591-1876ffc0dc9b)

The **CRM Migration Run** doctype tracks progress and per-step status as the migration executes.

![CRM Migration Run](https://github.com/user-attachments/assets/81870d55-a0c5-4967-8fb4-f102af2bc81d)

## Contributing

This app uses `pre-commit` for formatting + linting. Install with:

```bash
cd apps/erpnext_crm_to_frappe_crm_migrator
pre-commit install
```

Hooks: ruff, eslint, prettier, pyupgrade.

## License

AGPL-3.0

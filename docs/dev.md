# Operations & dev helpers

## Settings form workflow

`/app/crm-migration-settings` is the single user-facing surface.

| Action | Triggers |
|---|---|
| **Refresh Diff** (header button) | Walks every source doctype's meta, builds the per-tab Field Row table, and a hidden JSON of auto-mapped fields. Locked tabs are preserved untouched. |
| **Mark all as Skip** (per-tab grid button) | Server endpoint sets `action=Skip` on every blank row in that tab. |
| **Lock & Freeze \<Source\>** (per-tab) | Validates the tab's Field Row table (no blanks), commits the Mapped rows to `CRM Migration Field Map`, sets `<src>_locked = 1`. |
| **Unlock \<Source\>** (per-tab) | Clears the lock flag. Existing Field Map rows are kept until the next lock replaces them. |
| **Unlock All** (Mapping submenu; only when at least one tab is locked) | Clears the lock flag on every currently-locked tab in one save. |
| **Migrate \<Source\>** (per-tab) | Enqueues a single-source migration. Pre-flight refuses if upstream lookups aren't migrated yet. |
| **Run Migration** (top-level; only when all tabs locked) | Enqueues every source in dependency order, then runs the activity rewrite. |
| **Migrate Activity Records** (top-level; only when all tabs locked) | Enqueues just the activity rewrite. Used when parent migrations have already run and only the reference flips are needed. |
| **Default: Skip All & Migrate** (top-level) | One-click: Refresh Diff → Mark all unmapped as Skip → Lock every tab → Run full migration. Convenience for clean cutover. |
| **Undo migration** (top-level red; only when all tabs locked) | Reverts every target-side write — see [Undo migration](#undo-migration) below. |
| **Clean up ERPNext source data** (top-level red; only when all tabs locked) | Post-migration cleanup. See below. |

## Undo migration

The red "Undo migration" button (`api/undo.py`) reverts every target-side write the migrator made and deletes the CRM target rows it created. ERPNext source data is left untouched (source-meta preservation guarantees it's still there), so re-running the migration recreates everything cleanly.

**Guards (server-side):**
- All 8 tabs locked.
- ERPNext source rows still exist on at least one of `Lead` / `Opportunity` / `Prospect`. After cleanup deletes them, undo refuses — reverting activity refs would otherwise create orphan references.
- User typed the literal word `DELETE` in the confirmation field.

**Phases (in order — each produces one Run Step row per affected doctype for live progress):**

1. **Activity references** — `activity.revert_all_activities()` flips `reference_doctype` on FCRM Note / CRM Task / CRM Call Log / Comment / Communication / File / Version / Tag Link / View Log / DocShare / Event Participants / Notification Log / CRM SLA / ToDo (assignment-only) from CRM target back to ERPNext source.
2. **Dynamic Link revert** — flips `link_doctype` on Contact + Address links from CRM targets to source doctypes.
3. **Re-anchor revert** — flips `parenttype` on `CRM Status Change Log` (CRM Lead → Lead, CRM Deal → Opportunity) and `CRM Stage Change Log` (CRM Deal → Opportunity) back to source.
4. **Marker-tagged synthetic rows — deleted** — `FCRM Note` where `custom_source_crm_note IS NOT NULL`, `CRM Task` where `custom_source_todo IS NOT NULL`, plus the assignment ToDos those CRM Tasks own.
5. **Synth assignment ToDos — deleted** — open ToDos where `reference_type` is a CRM-side target (rows `reshape_assignments` synthesised from the `_assign` JSON cache).
6. **Reshape children on CRM Deal — deleted** — `CRM Products`, `CRM Contacts`, `CRM Rolling Response Time`.
7. **CRM parent target rows — deleted** — `CRM Lead`, `CRM Deal`, `CRM Organization`, `CRM Territory`, `CRM Industry`, `CRM Lead Source`, `CRM Lost Reason`, `CRM Product`.

**What's NOT touched:**
- `CRM Migration Run` / `CRM Migration Run Step` history — kept for audit.
- The `CRM Migration Field Map` rows — kept so the mapping survives across undo/re-migrate cycles.
- ERPNext-side rows — preserved by source-meta preservation; the source IS the rollback target.

**Preview endpoint:** `api/undo.get_undo_preview` returns counts for every phase without making any changes; the confirmation dialog renders these so the user knows exactly what's about to be touched.

## Post-migration cleanup

The red "Clean up ERPNext source data" button removes the original ERPNext rows once the migration is verified complete.

**Guards (server-side, re-checked on the actual delete call):**
- All 8 tabs locked.
- At least one `CRM Migration Run` with `status='Succeeded'`.
- User typed the literal word `DELETE` in the confirmation field.

**What gets deleted:**
- Parents: `Lead`, `Opportunity`, `Prospect`, `Opportunity Lost Reason`.
- Children: `CRM Note`, `Opportunity Item`, `Opportunity Lost Reason Detail`, `CRM Status Change Log` (leftover parenttype=Lead/Opp), `CRM Stage Change Log`, `Prospect Lead`, `Prospect Opportunity`, `Competitor Detail`.

**What is intentionally left alone (shared with other ERPNext modules):**
- `Item` — used by Stock, Manufacturing, Sales, Purchase.
- `Territory` — used by Customer, Sales Invoice, Selling Settings.
- `Industry Type` — used by Customer.
- `UTM Source` — used by Sales Invoice and marketing analytics.

Dynamic Link rows on Contact + Address are repointed in place by `reshape_dynamic_links` during the migration, not deleted here.

**Cleanup ↔ Undo are mutually exclusive:** running cleanup invalidates undo (no source rows left to revert references to). The undo endpoint detects this and refuses.

## Background-job names

The migration runs via `frappe.enqueue` on the `long` queue (6-hour timeout). To find specific jobs:

```bash
bench --site <site> execute frappe.db.sql \
  --kwargs '{"query": "SELECT name, status, queue, creation FROM `tabRQ Job` WHERE method LIKE \"%erpnext_crm_to_frappe_crm_migrator%\" ORDER BY creation DESC LIMIT 10"}'
```

Method paths in the enqueue trail:
- `erpnext_crm_to_frappe_crm_migrator.api.runner._execute_run` — Run Migration / Migrate \<Source\>
- `erpnext_crm_to_frappe_crm_migrator.api.runner._execute_activity_run` — Migrate Activity Records
- `erpnext_crm_to_frappe_crm_migrator.api.undo._execute_undo_run` — Undo migration

`/app/crm-migration-run` is the authoritative source of progress regardless of queue state.

## Failure triage

Every per-row / per-chunk / reshape failure writes a tracebacked Error Log entry with title prefix `Migrator …`:

| Title prefix | Source |
|---|---|
| `Migrator: <Source> row <name>` | per-row `_build_target_row` exception |
| `Migrator: <Source> bulk_insert chunk @ offset N` | whole-chunk insert failure (usually schema mismatch / FK) |
| `Migrator: <Source> chunk read` | source-side chunk SELECT failed |
| `Migrator reshape: <key>` | reshape per-row exception (`_bump_failed`) |
| `Migrator reshape: CRM Products bulk_insert` etc. | reshape chunk-level failure |
| `Migrator activity rewrite: <DocType> (<source>)` | activity-rewrite per-source failure |
| `Migrator activity revert: <DocType> (<target>)` | undo activity-revert per-source failure |
| `Undo: <phase>` | undo-pipeline per-phase failure (`api/undo`) |
| `Migrator step <Source> crashed` | unhandled crash inside the orchestrator step |

The Run Step row stores a truncated `last_error` + `sample_failed_names` for at-a-glance triage; Error Log entries have the full traceback.

## Dev helpers

Underscored helpers at the app root — not user-facing, kept for dev iteration:

| Helper | Purpose |
|---|---|
| `_reset_test_data.reset_all` | Wipe every CRM target row, revert the activity rewrite, drop Run history. Preserves the locked `CRM Migration Field Map` and per-tab Settings state so mapping work survives across resets. |
| `_reset_test_data.reset_deal_only` | Same shape but scoped to the CRM Deal pipeline. Reverts the parenttype re-anchor for CRM Status Change Log rather than deleting (so source data isn't destroyed). |
| `_debug_opp.run_inline` | Create a Run record and execute the Opportunity step synchronously (bypasses enqueue). Prints the Run summary + recent Error Log entries from the run window. |
| `_debug_utm.run_inline` | Same shape for the UTM Source step. |
| `_dedup_assign.run` | One-shot retroactive dedup of `_assign` / `_user_tags` / `_liked_by` JSON lists on already-migrated rows. Idempotent. |

Invoke via `bench --site <site> execute <dotted.path>`. Example:

```bash
bench --site crm.localhost execute \
  erpnext_crm_to_frappe_crm_migrator._reset_test_data.reset_deal_only

bench --site crm.localhost execute \
  erpnext_crm_to_frappe_crm_migrator._debug_opp.run_inline
```

## Dev helpers vs production endpoints

The dev-only `_reset_test_data.reset_all` is the **historical reference implementation** for the user-facing "Undo migration" button. They cover the same intent, but with two differences:

- `reset_all` also wipes `CRM Migration Run` / `CRM Migration Run Step` history; the production undo keeps it for audit.
- `reset_all` runs synchronously via `bench execute` with no Run Step rows; the production undo runs in the background queue and writes per-phase Run Step rows for live progress at `/app/crm-migration-run`.

When iterating on undo logic during development, change `api/undo.py` (production) — `_reset_test_data.py` stays as the original test reference.

## Manual cleanup beyond the button

The cleanup button covers the CRM-only source doctypes. For anything else (shared lookups, Dynamic Links pointing at uninstalled doctypes, orphan activity records), use targeted SQL via:

```bash
bench --site <site> execute frappe.db.sql --kwargs '{"query": "..."}'
```

Always preview row counts before deleting.

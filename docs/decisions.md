# Implementation decisions

The non-obvious calls the migrator makes, and why.

## Source-meta preservation

The runner carries `name`, `owner`, `creation`, `modified`, `modified_by`, `docstatus` verbatim from each source row to the target row. The trade-off vs. letting Frappe auto-name and auto-stamp:

| Benefit | Cost |
|---|---|
| Audit trail survives (who created / when, identical on both sides) | Target naming-series mismatch — `OPP-2024-00001` stays `OPP-2024-00001` on `CRM Deal`, not `CRM-DEAL-2024-00001` |
| Re-runs are idempotent — same primary key + `ignore_duplicates=True` | — |
| Activity rewrite only needs to flip `reference_doctype`; `reference_name` is unchanged | — |
| Cross-doctype linkages (e.g. `Contact.links.link_name`) resolve against the migrated row without translation | — |

The naming-series mismatch is the explicit choice — any external document (Sales Order, Quotation, custom report) that references the source name keeps resolving against the migrated row.

## `frappe.db.bulk_insert` instead of `frappe.get_doc().insert()`

For both the parent records and most reshapes, the migrator preprocesses each row in Python (building a column-aligned tuple) and uses `frappe.db.bulk_insert(..., ignore_duplicates=True)`. This skips:

- per-row validators
- doc events / hooks (`before_save`, `after_insert`, etc.)
- autoname callbacks
- link validation

10–100× throughput improvement on the parent migrations. Acceptable because:

- Target site is fresh; the locked field map is the only validation that matters.
- Source data has already passed ERPNext-side validation when it was first written.
- Things that genuinely need validation (Lead → Customer conversion) are out of scope for migration.

Exception: `reshape_tasks` previously used `task.insert()` so CRM Task's `after_insert` would create the assignment ToDo. The chain crashed on stale User Permission rows referencing uninstalled doctypes — switched to bulk_insert + synthesise the assignment ToDo in a second pass. See [`api/reshape.py:reshape_tasks`].

## `name` allocation for autoincrement targets

CRM Task uses `autoname=autoincrement`, backed by a MariaDB sequence. `bulk_insert` doesn't tap the sequence — without an explicit `name`, every row would land with `name=0` and `ignore_duplicates` would drop all but the first. Solution: pre-allocate each row's name via `frappe.database.sequence.get_next_val("CRM Task")` and include `name` in the bulk_insert columns.

## CRM Note name preservation

Source `CRM Note.name` is `bigint(20)` (autoincrement). FCRM Note's `name` column is `varchar(140)` — a stringified integer fits cleanly. Earlier code prefixed migrated rows with `mig-note-` on the reasoning that the types were incompatible; they aren't. With the prefix, 21 Comments + 370 Versions referencing `CRM Note` by name would land on non-existent FCRM Note rows after the activity rewrite. Current behavior: source name preserved verbatim; a hidden `custom_source_crm_note` Custom Field tags migrator-created rows for the reset script.

## Dynamic Link repoint (not "add new rows")

Earlier `reshape_prospect_contacts` *added* new `tabDynamic Link` rows on Contact pointing at CRM Organization, leaving the source-pointing rows in place. Issues with that approach:

1. Doubled the dataset (4,748 → 9,496 on the test bench).
2. Only covered Contact-side Prospect linkages — ignored Address-side and the Lead/Opportunity equivalents (5,128 rows orphaned after cleanup).
3. After cleanup deletes the source rows, the original Dynamic Link rows become orphans.

Replaced with `reshape_dynamic_links` — 6 single-statement UPDATEs that *repoint* in place:

```
Contact + Address × (Lead / Opportunity / Prospect) → (CRM Lead / CRM Deal / CRM Organization)
```

`link_name` is preserved (source name == target name from source-meta preservation), so the linkage continues to resolve against the migrated CRM-side row. No doubling, full coverage, no orphans after cleanup.

The trade-off: ERPNext-side queries against the source doctypes break the moment migration runs. Acceptable because the rollout plan is migrate → verify → cleanup; reports were going to break at cleanup anyway.

## Customer → CRM Organization bridge (target-side only)

For `opportunity_from='Customer'` Opportunities, `CRM Deal.organization` needs a value. ERPNext-side `Customer` has no equivalent in Frappe CRM. Earlier the bridge inserted both a Prospect (source side) and a CRM Organization (target side); the Prospect insert violated the policy of never writing to ERPNext source.

Current behavior in `_ensure_customer_organizations`: only the CRM Organization is lazy-created, named after `Customer.customer_name` (or `Customer.name` as fallback). The chunk loop rewrites `Opportunity.party_name` from `Customer.name` to the org name; dynamic-link routing sends it to `CRM Deal.organization`. Plus the original `Customer.name` is stashed on `CRM Deal.erpnext_customer` (a Data field Frappe CRM's ERPNext integration installs) if that column exists.

## Skipping target-side validation on bulk_insert

`bulk_insert` won't run `validate()`. The migrator relies on three categories of pre-flight to compensate:

1. **Pre-flight Link check** (`_preflight_link_check`) — for every Link field in the locked map whose target options differ between source and target, verify every distinct source value exists in the target lookup table. Fails the step early with a clear error rather than landing orphan FKs.
2. **Lazy lookup creation** (in reshapes) — `_ensure_crm_product`, `_ensure_crm_lost_reason`, `_ensure_customer_organizations` create missing dependent records on demand.
3. **JSON list dedup** (`_dedup_json_list`) — `_assign` / `_user_tags` / `_liked_by` may carry duplicate entries on the source; deduped at write time to avoid doubled avatars in the CRM UI.

## Failure handling

Every per-row, per-chunk, per-reshape exception writes a full traceback to the Error Log with a `Migrator …` title prefix. The Run Step row keeps a truncated `last_error` and a comma-separated `sample_failed_names` for a quick read; the Error Log is the source of truth for full tracebacks.

A step with `failed_count > 0` ends `Failed`; one with `child_failed_count > 0` ends `Failed` too. The orchestrator does NOT stop on the first bad row — it logs and continues — so a partial-failure run still produces as much migrated data as possible.

## Status & sales-stage on CRM Deal

CRM Deal has both `status` (Link → `CRM Deal Status`: Open/Won/Lost/Stalled/Not Pursued) and `sales_stage` (Link → `Sales Stage`). The registry maps:

- `Opportunity.status → CRM Deal.status`
- `Opportunity.sales_stage → CRM Deal.sales_stage` (same-name auto via the frappe_crm_xt mirror)

Source `Opportunity.status` values (Open/Quotation/Converted/Lost/Replied/Closed) only partially overlap with CRM Deal Status — `Open` is the only natural match. Value translation is currently a manual step (the user must add CRM Deal Status rows for the other values, or set up a value-translation map). See [`mapping.md`](mapping.md) "Value-translation gaps".

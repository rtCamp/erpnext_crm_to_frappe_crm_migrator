# Architecture

A one-time cutover from ERPNext CRM to Frappe CRM on the same site. Four phases run end-to-end in order; the user gates the whole thing via locked field mappings on the `CRM Migration Settings` Single doctype.

## Steps

| # | What it does | Where it lives |
|---|---|---|
| 1 | **Mapping UI + diff** — per source doctype, walk meta (custom fields included) and propose target field via registry or same-name match. The user resolves anything ambiguous, then locks each tab. | `api/mapping.py` |
| 2 | **Core records migration** — read locked `CRM Migration Field Map`, bulk-insert target rows in dependency order (lookups → parents), preserve source `name`/owner/creation/etc. | `api/runner.py` |
| 3 | **Schema reshape** — populate Table-typed children and cross-doctype relationships that need a structural change (Items, Contacts, Lost Reasons, Notes, ToDos, Stage Logs, Dynamic Links). | `api/reshape.py` |
| 4 | **Activity reference rewrite** — flip `reference_doctype` on Comments/ToDos/FCRM Notes/Versions/etc. from ERPNext source values to CRM target values. Reference names are unchanged. | `api/activity.py` |

Plus two opt-in buttons covered in `dev.md`:
- **Undo migration** (`api/undo.py`) — reverts every target-side write of phases 2–4 and drops the CRM target rows. ERPNext source rows are left intact, so re-running the migration is non-destructive. Refuses if cleanup already deleted the source rows.
- **Clean up ERPNext source data** (`api/cleanup.py`) — deletes the ERPNext source rows once the cutover is verified. Mutually exclusive with Undo.

## Source / target pairs

| ERPNext source | Frappe CRM target |
|---|---|
| Lead | CRM Lead |
| Opportunity | CRM Deal |
| Prospect | CRM Organization |
| Territory | CRM Territory |
| Industry Type | CRM Industry |
| UTM Source | CRM Lead Source |
| Opportunity Lost Reason | CRM Lost Reason |
| Item | CRM Product |

## Dispatch order

Lookups before parents so Link references resolve:

```
Industry Type → UTM Source → Opportunity Lost Reason → Territory → Item
              → Prospect → Lead → Opportunity
```

## Reshape pipeline

The reshape step runs different functions per source doctype. The order matters — children with cross-doctype dependencies need their target parents to exist first.

| Source | Reshape functions invoked, in order |
|---|---|
| Lead | `reshape_notes` → `reshape_assignments` → `reshape_tasks` |
| Prospect | `reshape_notes` → `reshape_assignments` → `reshape_tasks` |
| Opportunity | `reshape_opportunity_items` → `reshape_opportunity_contacts` → `reshape_opportunity_lost_reasons` → `reshape_notes` → `reshape_assignments` → `reshape_tasks` → `reshape_dynamic_links` |

`reshape_dynamic_links` runs once at the end of the Opportunity step (the last source in dependency order) so the Contact/Address `.links` repoint resolves against the full set of migrated targets.

## Shared-child re-anchor vs reshape

Two patterns the migrator uses for child-table data:

- **Re-anchor** (in `runner.py`, `_reanchor_shared_children`): when the source and target Table fields share the *same* child doctype (e.g. `Lead.status_change_log` and `CRM Lead.status_change_log` both → `CRM Status Change Log`, or `Opportunity.custom_stage_change_log` and `CRM Deal.custom_stage_change_log` both → `CRM Stage Change Log`), flip `parenttype` on the child rows in one UPDATE. No re-insert, child meta preserved. Silently skips when the target side doesn't carry the field — useful for sites without `frappe_crm_xt`'s `CRM Stage Change Log` doctype.
- **Reshape** (in `reshape.py`): when the child doctype itself changes (e.g. Opportunity Item → CRM Products), copy rows across tables on the mapped columns.

See [`mapping.md`](mapping.md) for the field-level details and [`decisions.md`](decisions.md) for the rationale behind the source-meta preservation, bulk_insert, and repoint choices.

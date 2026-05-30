# Field mapping

How each source field reaches its target column.

## Resolution order

When refreshing the diff, each source field is resolved in this order:

1. **Registry hit** — the `SUGGESTION_MAP` in `mapping/registry.py` (e.g. `CRM_LEAD_TO_LEAD`, `CRM_DEAL_TO_OPPORTUNITY`) names an explicit target column. Used for cross-system renames.
2. **Same-name exact match** — if the target doctype has a field with the same name (native or custom), auto-map.
3. **Registry target missing on the target meta** — flagged as `Unresolved` with a risk note.
4. **No candidate at all** — flagged as `Unresolved`; the user picks Skip or Map manually.

The exact-name fallback is what makes the **target-side custom-field mirror** pattern work — `frappe_crm_xt/setup/custom_fields.json` installs same-name Custom Fields on CRM Lead / CRM Deal / CRM Organization / FCRM Note / Sales Stage so the diff auto-maps them without further registry entries.

## Registry shape

Defined in `mapping/registry.py`. Each `<CRM>_TO_<ERPNEXT>` dict maps target column → source column; the inverse (used at write time) is computed via dict comprehension.

```python
CRM_LEAD_TO_LEAD = {
    "first_name": "first_name",
    "organization": "company_name",   # cross-system rename
    ...
}
LEAD_TO_CRM_LEAD = {v: k for k, v in CRM_LEAD_TO_LEAD.items()}
```

The `SUGGESTION_MAP` dict ties each source doctype to its inverse:

```python
SUGGESTION_MAP = {
    "Lead":        LEAD_TO_CRM_LEAD,
    "Opportunity": OPPORTUNITY_TO_CRM_DEAL,
    ...
}
```

## Special routing mechanisms (outside the field map)

These bypass the locked Field Map entirely because the source/target relationship is more complex than a column rename:

### Dynamic-Link routing (`DYNAMIC_LINK_ROUTES` in `api/mapping.py`)

For source fields whose value lands on different target columns depending on a sibling "controller" field. Example: `Opportunity.party_name` routes to either `CRM Deal.lead` or `CRM Deal.organization` based on `opportunity_from`:

| Controller value | Target column |
|---|---|
| `Lead` | `CRM Deal.lead` |
| `Customer` | `CRM Deal.organization` (via Customer→Organization bridge) |
| `Prospect` | `CRM Deal.organization` |

`opportunity_from` itself is filtered from the decision table — its data is consumed by the router, not column-mapped.

### Name routing (`SUGGESTION_MAP[source]["name"]` lookup in `api/runner.py`)

For doctypes where the source's `name` (autoname=prompt or autoincrement) is the canonical user-facing label and the target has a separate column for it. Example: `UTM Source.name → CRM Lead Source.source_name`. `name` is never surfaced in the diff (it's a framework field), so the runner consults the registry directly at setup time.

### `RESHAPE_HANDLED_FIELDS` (in `mapping/registry.py`)

Source fields whose data lands on the target via a reshape function rather than a column copy. Hidden from the per-tab decision table so the user isn't asked to resolve a row that's already covered. Currently: `Opportunity.order_lost_reason` (folded into `lost_notes` by `reshape_opportunity_lost_reasons`).

## Target-side custom-field mirrors

Frappe CRM's stock schema lacks columns for many ERPNext-side fields the customer wants to preserve. `frappe_crm_xt` installs same-name Custom Fields on the CRM doctypes so the migrator's same-name fallback picks them up automatically — no registry entry needed.

Notable mirrors:

| Target | Field | Source on |
|---|---|---|
| CRM Deal | `sales_stage` (Link → Sales Stage) | `Opportunity.sales_stage` |
| CRM Deal | `transaction_date` (Date) | `Opportunity.transaction_date` |
| CRM Deal | `title`, `company`, `country`, `city`, `state`, `customer_address`, `address_display` | same on Opportunity |
| CRM Deal | `custom_priority` | `Opportunity.custom_priority` (next_crm) |
| CRM Deal | `custom_last_response_by` | `Opportunity.custom_last_response_by` (rtcamp) |
| FCRM Note | `custom_note_attachments` (Table → NCRM Attachments) | `CRM Note.custom_note_attachments` |
| FCRM Note | `custom_parent_note` (Link → FCRM Note) | `CRM Note.custom_parent_note` |
| Sales Stage | `custom_checklist` (Table → CRM Deal Status Checklist) | re-anchored from `Opportunity Status Checklist` |
| Customer | All MSA / insurance / contract date customs | preserved on the same `tabCustomer` post-uninstall of next_crm |

## Cross-system renames (registry, not same-name)

A handful of source fields are explicitly renamed via the registry because the target doctype uses a different fieldname for the same concept:

| Source | Target |
|---|---|
| `Lead.company_name` | `CRM Lead.organization` |
| `Lead.lead_name` | `CRM Lead.first_name` / `last_name` (resolved by Frappe CRM's controller) |
| `Opportunity.opportunity_amount` | `CRM Deal.deal_value` |
| `Opportunity.opportunity_owner` | `CRM Deal.deal_owner` |
| `Opportunity.expected_closing` | `CRM Deal.expected_closure_date` |
| `Opportunity.customer_name` | `CRM Deal.organization_name` |
| `Opportunity.contact_email` | `CRM Deal.email` |
| `Opportunity.contact_mobile` | `CRM Deal.mobile_no` |
| `Opportunity.custom_won_date` | `CRM Deal.closed_date` (matches Frappe CRM's "set when status type = Won" semantics) |
| `Lead.custom_last_responded_on` / `Opportunity.custom_last_responded_on` | `CRM Lead.last_responded_on` / `CRM Deal.last_responded_on` |
| `Prospect.company_name` | `CRM Organization.organization_name` |
| `UTM Source.name` | `CRM Lead Source.source_name` (via the name-routing mechanism) |
| `UTM Source.description` | `CRM Lead Source.details` |

## Value-translation gaps

The migrator only renames columns — it does not translate **values**. Two known gaps:

- `Opportunity.status` (`Open` / `Quotation` / `Converted` / `Lost` / `Replied` / `Closed`) lands verbatim on `CRM Deal.status`. Only `Open` overlaps natively with CRM Deal Status (`Open` / `Won` / `Lost` / `Stalled` / `Not Pursued`). The rest are invalid Link references unless matching `CRM Deal Status` rows have been added on the target side.
- `Lead.status` is mostly aligned with CRM Lead Status on customer benches (since the customer matched names during their ERPNext usage), but stragglers like `Opportunity` will land as invalid.

A future enhancement could be a registered value-translation map (or an optional pre-migration step to materialise CRM Deal Status rows from existing Sales Stage values — see `dev.md` discussion).

## Activity rewrite scope

`api/activity.py` rewrites `reference_doctype`-equivalent columns across this set of doctypes:

```
FCRM Note, CRM Task, CRM Call Log, CRM Notification, Comment, Communication,
File, Version, Communication Link, Tag Link, View Log, DocShare,
Event Participants, Notification Log, CRM Service Level Agreement
```

Plus a special-cased ToDo rewrite — only assignment-style ToDos (description starts with `Assignment for …`) get their `reference_type` flipped, because content ToDos become CRM Tasks via `reshape_tasks` instead.

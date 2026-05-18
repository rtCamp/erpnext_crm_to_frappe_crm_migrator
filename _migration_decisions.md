# Migration field-mapping decisions

Park file for per-doctype field decisions during the ERPNext-CRM →
Frappe-CRM migration setup. Resume from here when continuing the audit.

## Status

- **Lead** — discussion in progress, 2 fields awaiting decision.
- **Opportunity** — not yet discussed.
- **Prospect** — not yet discussed.
- **Item** — not yet discussed.
- Lookups (Territory, Industry Type, UTM Source, Opportunity Lost Reason) — already covered, no unresolved fields.

## Cross-cutting (done before per-doctype audit)

- **Orphan `source` column on Lead/Opportunity** — `frappe.get_meta` couldn't see it; data would have been silently dropped (58 Leads, 1678 Opportunities). Upstream ERPNext PR opened: https://github.com/frappe/erpnext/pull/55022 (`fix: backfill utm_source from legacy source column`). Patch has already run on this bench — both `utm_source` columns are now populated; registry's `utm_source → source` mapping handles the rest.

## Lead → CRM Lead

542 source rows. 43 auto-mapped (registry + same-name), 23 still
"needs decision" in the per-tab table.

### Pending decisions (act on these)

| Field | Rows / Distinct | Action | Notes |
|---|---|---|---|
| `type` | 18 / 2 (Client 11, Channel Partner 7) | **DECIDE** | Skip or mirror as `custom_lead_type` Select on CRM Lead via frappe_crm_xt |
| `request_type` | 16 / 2 (Request for Info 15, Other 1) | **DECIDE** | Skip or mirror as `custom_request_type` Select on CRM Lead via frappe_crm_xt |

### Tentative Skips (recheck before locking)

| Field | Rows / Distinct | Why Skip |
|---|---|---|
| `country` | 542 / 4 | Frappe CRM models addresses via Address doctype on Contact; CRM Lead has no `country` column. Could reshape to Address rows if fidelity matters. |
| `title` | 542 / 541 | Frappe CRM derives display title from name/organization. Source `title` mostly redundant. |
| `qualification_status` | 542 / 2 (Unqualified 541, Qualified 1) | Redundant with `status` (Qualified/Unqualified already in CRM Lead Status). |
| `company` | 542 / 1 (all rtCamp) | No information content. |
| `language` | 542 / 1 (all 'en') | No information content. |
| `disabled` | 3 / 1 | Frappe CRM uses `status=Junk`/`Unqualified` for this. |
| `city`, `state`, `qualified_by`, `qualified_on` | 1 each | Negligible volume + no direct target column. |
| `blog_subscriber`, `customer`, `fax`, `market_segment`, `phone_ext`, `response_by`, `unsubscribed`, `utm_campaign`, `utm_content`, `utm_medium`, `whatsapp_no` | 0 each | No data. |

### Already decided / done

(none specific to Lead beyond what's in the cross-cutting section)

## Opportunity → CRM Deal

*Audit not yet walked — see "Open items" below.*

### Already decided / done

- `custom_priority` mirrored on CRM Deal: https://github.com/rtCamp/frappe_crm_xt/commit/c696813
- `custom_won_date` registry-renamed to `CRM Deal.closed_date`: https://github.com/rtCamp/erpnext_crm_to_frappe_crm_migrator/commit/77e7310
- `sales_stage` mirrored on CRM Deal (Link → Sales Stage); same-name auto-maps via the migrator's diff.
- `status`: registry maps `Opportunity.status → CRM Deal.status` (value alignment not done — only "Open" overlaps natively with `CRM Deal Status`; needs status-row additions or a value-translation map).

## Prospect → CRM Organization

*Audit not yet walked — see "Open items" below.*

## Item → CRM Product

*Audit not yet walked — see "Open items" below.*

## Open items (next sessions)

- Walk **Opportunity** unresolved fields (~28: `title`, `transaction_date`, `company`, `language`, `opportunity_type`, `base_opportunity_amount`, `country`, `order_lost_reason`, `customer_group`, plus the Address-typed fields).
- Decide value-translation for `Opportunity.status → CRM Deal Status` (or extend CRM Deal Status rows).
- Walk **Prospect** unresolved fields (2: `company`, `prospect_owner` 88%).
- Walk **Item** unresolved fields (18 — mostly ERP-only stock/asset).
- Decide on `Address` doctype reshape vs Skip for Lead/Opp `city/state/country/customer_address`.

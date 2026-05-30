"""Post-migration cleanup — delete ERPNext-side CRM source data.

Runs after a successful migration to wipe the original source rows
(`tabLead`, `tabOpportunity`, `tabProspect`, `tabOpportunity Lost Reason`)
and their children. Strictly opt-in via the Settings-form button —
never triggered automatically.

Doctypes shared with other ERPNext modules (Item, Territory, Industry
Type, UTM Source) are deliberately **not** included: deleting those
would break Customer, Sales Invoice, Stock, and other unrelated areas.
"""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_crm_to_frappe_crm_migrator.mapping.registry import SOURCE_DOCTYPES

SETTINGS_DOCTYPE = "CRM Migration Settings"
RUN_DOCTYPE = "CRM Migration Run"


# CRM-only source parents — safe to delete in full.
_PARENT_DOCTYPES: list[str] = [
	"Lead",
	"Opportunity",
	"Prospect",
	"Opportunity Lost Reason",
]

# Child tables anchored to the parents above. Each entry is
# (child_doctype, [parenttypes_to_clean]). Children are deleted before
# parents so we don't leave orphan rows.
_CHILD_DOCTYPES: list[tuple[str, list[str]]] = [
	("CRM Note", ["Lead", "Opportunity", "Prospect"]),
	("Opportunity Item", ["Opportunity"]),
	("Opportunity Lost Reason Detail", ["Opportunity"]),
	("CRM Status Change Log", ["Lead", "Opportunity"]),
	("CRM Stage Change Log", ["Opportunity"]),
	("Prospect Lead", ["Prospect"]),
	("Prospect Opportunity", ["Prospect"]),
	("Competitor Detail", ["Opportunity"]),
]

# Shared lookup masters — excluded from cleanup. Listed in the preview
# so the user knows they are intentionally left alone.
_SHARED_DOCTYPES: list[tuple[str, str]] = [
	("Territory", "Used by Customer, Sales Invoice, Sales Order, Selling Settings"),
	("Industry Type", "Used by Customer (not just Lead/Opportunity)"),
	("UTM Source", "Used by Sales Invoice and marketing-side analytics"),
	("Item", "Used by Stock, Manufacturing, Sales, Purchase"),
]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def _all_tabs_locked(settings) -> tuple[bool, list[str]]:
	"""Return (all_locked, list_of_unlocked_source_names)."""
	unlocked = [
		s for s in SOURCE_DOCTYPES
		if not settings.get(f"{s.lower().replace(' ', '_')}_locked")
	]
	return (not unlocked, unlocked)


def _has_successful_run() -> bool:
	if not frappe.db.exists("DocType", RUN_DOCTYPE):
		return False
	return bool(frappe.db.count(RUN_DOCTYPE, {"status": "Succeeded"}))


# ---------------------------------------------------------------------------
# whitelisted endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_cleanup_preview() -> dict:
	"""List source tables that would be deleted, with current row counts.

	Frontend uses this to build the confirmation dialog. Does not perform
	any deletes.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	all_locked, unlocked = _all_tabs_locked(settings)

	parents: list[dict] = []
	for dt in _PARENT_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		n = frappe.db.count(dt)
		if n:
			parents.append({"doctype": dt, "count": n})

	children: list[dict] = []
	for dt, parent_types in _CHILD_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		n = frappe.db.count(dt, {"parenttype": ["in", parent_types]})
		if n:
			children.append({
				"doctype": dt,
				"count": n,
				"parenttypes": parent_types,
			})

	skipped = [
		{"doctype": dt, "reason": reason, "count": frappe.db.count(dt) if frappe.db.exists("DocType", dt) else 0}
		for dt, reason in _SHARED_DOCTYPES
	]

	return {
		"ok": True,
		"all_locked": all_locked,
		"unlocked_tabs": unlocked,
		"has_successful_run": _has_successful_run(),
		"parents": parents,
		"children": children,
		"skipped": skipped,
	}


@frappe.whitelist()
def cleanup_source_data(confirm: str = "") -> dict:
	"""Delete CRM-only source data. Requires `confirm='DELETE'` and the
	guards (all tabs locked + at least one Succeeded run) to pass.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)

	if confirm != "DELETE":
		frappe.throw(
			_("Type DELETE to confirm this destructive operation."),
			title=_("Confirmation Missing"),
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	all_locked, unlocked = _all_tabs_locked(settings)
	if not all_locked:
		frappe.throw(
			_("Lock every tab before running cleanup. Unlocked: {0}").format(", ".join(unlocked)),
			title=_("Tabs Not Locked"),
		)

	if not _has_successful_run():
		frappe.throw(
			_("No successful migration run found. Run the migration to completion first."),
			title=_("Migration Not Complete"),
		)

	deleted: dict[str, int] = {}

	# 1. Children first — Frappe doesn't cascade.
	for dt, parent_types in _CHILD_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		filters = {"parenttype": ["in", parent_types]}
		n = frappe.db.count(dt, filters)
		if n:
			frappe.db.delete(dt, filters)
			deleted[f"child {dt}"] = n

	# 2. Parents. Dynamic Links on Contact / Address that pointed at
	# these doctypes were already re-pointed to the CRM-side equivalents
	# by `reshape_dynamic_links` during the migration, so nothing extra
	# to clean up here.
	for dt in _PARENT_DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		n = frappe.db.count(dt)
		if n:
			frappe.db.delete(dt)
			deleted[dt] = n

	frappe.db.commit()
	return {"ok": True, "deleted": deleted}

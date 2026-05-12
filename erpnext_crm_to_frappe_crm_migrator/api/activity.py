"""Phase 4 — activity reference rewrite.

Walks the activity doctypes (FCRM Note, CRM Task, CRM Call Log,
CRM Notification, Comment, Communication, File, ToDo) and rewrites their
reference-doctype field from ERPNext source values to the corresponding
Frappe CRM target values. Reference *names* are unchanged because Phase 2
preserves source `name` on target rows.

Also rewrites `CRM Service Level Agreement.apply_on`. Existing SLA rows
carry `apply_on = "Lead"` / `"Opportunity"`, but Frappe CRM's link_filters
on that field only accept `["CRM Lead", "CRM Deal"]`, so any SLA row left
unrewritten fails on next save. Same rewrite shape as the activity
doctypes, so it lives here.

Each doctype is its own step in the CRM Migration Run log. The runner
appends one CRM Migration Run Step per doctype with
`s_doctype = t_doctype = <doctype>` and counters for the rows updated.

Idempotency: re-runs match no rows because the WHERE filter targets only
source-doctype values; once rewritten the rows hold target-doctype values
and are no longer matched.
"""

from __future__ import annotations

import frappe

from erpnext_crm_to_frappe_crm_migrator.mapping.registry import REVERSE_DOCTYPE_MAP

# (activity_doctype, doctype-field, name-field). Order is the order rows
# appear in the Run log. name_field is informational — Phase 2 preserves
# source `name` on target rows, so only doctype_field is rewritten.
ACTIVITY_SPECS: list[tuple[str, str, str]] = [
	("FCRM Note", "reference_doctype", "reference_docname"),
	("CRM Task", "reference_doctype", "reference_docname"),
	("CRM Call Log", "reference_doctype", "reference_docname"),
	("CRM Notification", "reference_doctype", "reference_name"),
	("Comment", "reference_doctype", "reference_name"),
	("Communication", "reference_doctype", "reference_name"),
	("File", "attached_to_doctype", "attached_to_name"),
	("ToDo", "reference_type", "reference_name"),
	# Not an activity record, but the same single-column doctype-name
	# rewrite. Frappe CRM's apply_on link_filters reject Lead/Opportunity.
	("CRM Service Level Agreement", "apply_on", ""),
]


def rewrite_activity_doctype(
	activity_dt: str,
	doctype_field: str,
	name_field: str,
) -> dict:
	"""Rewrite reference_doctype on one activity doctype across all sources.

	Returns a result dict aligned with the runner step counters:
	  ok      = total rows updated across all source pairs
	  skipped = pairs with no matching source rows
	  failed  = pairs that errored out
	  last_error / sample_failed: most recent failure detail
	"""
	result: dict = {
		"ok": 0,
		"skipped": 0,
		"failed": 0,
		"last_error": "",
		"sample_failed": [],
	}

	if not frappe.db.exists("DocType", activity_dt):
		# DocType not installed on this site — nothing to rewrite.
		return result

	for source_dt, target_dt in REVERSE_DOCTYPE_MAP.items():
		try:
			# Count first so we know whether we did anything (UPDATE
			# affected_rows isn't reliably exposed across drivers).
			matched = frappe.db.count(activity_dt, {doctype_field: source_dt})
			if not matched:
				result["skipped"] += 1
				continue

			frappe.db.sql(
				f"""
				UPDATE `tab{activity_dt}`
				SET `{doctype_field}` = %s
				WHERE `{doctype_field}` = %s
				""",
				(target_dt, source_dt),
			)
			result["ok"] += matched
		except Exception as e:
			result["failed"] += 1
			result["last_error"] = f"{source_dt}: {e}"[:500]
			result["sample_failed"].append(f"{source_dt} → {target_dt}")

	frappe.db.commit()
	return result


def rewrite_all_activities() -> list[tuple[str, dict]]:
	"""Rewrite all 8 activity doctypes. Returns [(activity_dt, result), ...]
	in stable order so the runner can create matching CRM Migration Run
	Step rows.
	"""
	return [
		(dt, rewrite_activity_doctype(dt, fld_dt, fld_name))
		for (dt, fld_dt, fld_name) in ACTIVITY_SPECS
	]

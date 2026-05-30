"""Activity reference rewrite — flips `reference_doctype` (and aliases)
on activity records from ERPNext source values to CRM target values.

Reference names are preserved by the core records runner, so only the
doctype column needs to change. See `docs/mapping.md` for the full
scope of doctypes touched.
"""

from __future__ import annotations

import frappe

from erpnext_crm_to_frappe_crm_migrator.mapping.registry import REVERSE_DOCTYPE_MAP

# (activity_doctype, doctype-field, name-field). Order is the order rows
# appear in the Run log. name_field is informational — source `name` is
# preserved on target rows, so only doctype_field is rewritten.
ACTIVITY_SPECS: list[tuple[str, str, str]] = [
	("FCRM Note", "reference_doctype", "reference_docname"),
	("CRM Task", "reference_doctype", "reference_docname"),
	("CRM Call Log", "reference_doctype", "reference_docname"),
	("CRM Notification", "reference_doctype", "reference_name"),
	("Comment", "reference_doctype", "reference_name"),
	("Communication", "reference_doctype", "reference_name"),
	("File", "attached_to_doctype", "attached_to_name"),
	# Document-version history — keeps the audit trail visible on the
	# migrated CRM doc page. tabVersion rows have ref_doctype + docname.
	("Version", "ref_doctype", "docname"),
	# Communication's linked-docs side-table. Each Communication can be
	# linked to multiple docs; without this rewrite the email timeline
	# on the migrated CRM doc won't surface those threads even though
	# Communication itself has been rewritten.
	("Communication Link", "link_doctype", "link_name"),
	# Tags applied to source docs. Without this, the Tags filter on the
	# migrated CRM list views won't pick them up.
	("Tag Link", "document_type", "document_name"),
	# "Recently viewed" widget + per-doc view history.
	("View Log", "reference_doctype", "reference_name"),
	# Explicit per-doc share permissions (the desk Share button).
	("DocShare", "share_doctype", "share_name"),
	# Calendar events linked to a source doc.
	("Event Participants", "reference_doctype", "reference_docname"),
	# Empty on most sites but cheap to keep idempotent.
	("Notification Log", "document_type", "document_name"),
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

			table = frappe.qb.DocType(activity_dt)
			(
				frappe.qb.update(table)
				.set(table[doctype_field], target_dt)
				.where(table[doctype_field] == source_dt)
				.run()
			)
			result["ok"] += matched
		except Exception as e:
			result["failed"] += 1
			result["last_error"] = f"{source_dt}: {e}"[:500]
			result["sample_failed"].append(f"{source_dt} → {target_dt}")
			frappe.log_error(
				title=f"Migrator activity rewrite: {activity_dt} ({source_dt})",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist per-doctype activity rewrites so the next doctype sees a consistent state
	return result


def rewrite_assignment_todos() -> dict:
	"""Rewrite reference_type ONLY on Frappe-assignment-style ToDos.

	Frappe's `assign_to` API emits ToDo rows whose description begins with
	`"Assignment for <DocType> <name>"`. Those rows have no body content of
	their own — they're a side-channel for the desk's assignment widget —
	so the right migration is to flip their `reference_type` from
	Lead/Opportunity/Prospect to the CRM equivalent, in place.

	Content ToDos (description doesn't start with "Assignment for ") are
	intentionally left alone. Their migration is the CRM Task created by
	reshape_tasks; rewriting `reference_type` on top would leave the user
	with both a CRM Task and a stale ToDo pointing at the same CRM doc.
	"""
	result: dict = {
		"ok": 0,
		"skipped": 0,
		"failed": 0,
		"last_error": "",
		"sample_failed": [],
	}

	todo = frappe.qb.DocType("ToDo")
	for source_dt, target_dt in REVERSE_DOCTYPE_MAP.items():
		try:
			matched = frappe.db.count(
				"ToDo",
				{
					"reference_type": source_dt,
					"description": ["like", "Assignment for %"],
				},
			)
			if not matched:
				result["skipped"] += 1
				continue

			(
				frappe.qb.update(todo)
				.set(todo.reference_type, target_dt)
				.where((todo.reference_type == source_dt) & (todo.description.like("Assignment for %")))
				.run()
			)
			result["ok"] += int(matched)
		except Exception as e:
			result["failed"] += 1
			result["last_error"] = f"{source_dt}: {e}"[:500]
			result["sample_failed"].append(f"{source_dt} → {target_dt}")
			frappe.log_error(
				title=f"Migrator activity rewrite: ToDo assignment ({source_dt})",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist per-doctype activity rewrites so the next doctype sees a consistent state
	return result


def rewrite_all_activities() -> list[tuple[str, dict]]:
	"""Rewrite all activity doctypes. Returns [(activity_dt, result), ...]
	in stable order so the runner can create matching CRM Migration Run
	Step rows.

	ToDo is special-cased — only assignment ToDos get their reference_type
	rewritten. See `rewrite_assignment_todos` for the rationale.
	"""
	out: list[tuple[str, dict]] = [
		(dt, rewrite_activity_doctype(dt, fld_dt, fld_name)) for (dt, fld_dt, fld_name) in ACTIVITY_SPECS
	]
	out.append(("ToDo", rewrite_assignment_todos()))
	return out

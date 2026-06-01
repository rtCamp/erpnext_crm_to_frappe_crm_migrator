"""Undo migration — revert activity references AND drop every target-side
row the migrator created (CRM Lead / CRM Deal / CRM Organization parents,
their reshape-synthesised children, marker-tagged FCRM Notes / CRM Tasks).

Opposite of `api/cleanup` which deletes source-side rows after migration:
this restores the site to its pre-migration state on the target side.
Requires the ERPNext source rows to still exist — after cleanup has
deleted them, this endpoint refuses because the activity-reference revert
would leave orphan refs.

See `docs/dev.md` for the dev-helper reference implementation
(`_reset_test_data.py`) that this productionises.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_crm_to_frappe_crm_migrator.api import activity
from erpnext_crm_to_frappe_crm_migrator.mapping.registry import (
	REVERSE_DOCTYPE_MAP,
	SOURCE_DOCTYPES,
)

SETTINGS_DOCTYPE = "CRM Migration Settings"
RUN_DOCTYPE = "CRM Migration Run"


# CRM-side parent target doctypes — deleted in step 6.
_TARGET_PARENTS: list[str] = [
	"CRM Lead",
	"CRM Deal",
	"CRM Organization",
	"CRM Territory",
	"CRM Industry",
	"CRM Lead Source",
	"CRM Lost Reason",
	"CRM Product",
]

# Reshape-synthesised child doctypes under CRM Deal (no source-side
# equivalent — pure new rows created by reshape functions).
_RESHAPE_TARGET_CHILDREN: list[tuple[str, list[str]]] = [
	("CRM Products", ["CRM Deal"]),
	("CRM Contacts", ["CRM Deal"]),
	("CRM Rolling Response Time", ["CRM Lead", "CRM Deal"]),
]

# Marker-tagged synthetic rows the reshape step created. Each entry is
# (target_doctype, marker_fieldname) — rows where the marker is set are
# migrator-created and safe to delete; user-created rows don't carry it.
_MARKER_TAGGED_DELETES: list[tuple[str, str]] = [
	("FCRM Note", "custom_source_crm_note"),
	("CRM Task", "custom_source_todo"),
]

# Shared-schema child tables whose `parenttype` got flipped from source
# to target by `_reanchor_shared_children`. We flip them back to source.
_REANCHORED_CHILDREN: list[tuple[str, str, str]] = [
	# (child_dt, target_parenttype, source_parenttype)
	("CRM Status Change Log", "CRM Lead", "Lead"),
	("CRM Status Change Log", "CRM Deal", "Opportunity"),
	("CRM Stage Change Log", "CRM Deal", "Opportunity"),
]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def _all_tabs_locked(settings) -> tuple[bool, list[str]]:
	unlocked = [s for s in SOURCE_DOCTYPES if not settings.get(f"{s.lower().replace(' ', '_')}_locked")]
	return (not unlocked, unlocked)


def _source_rows_remain() -> bool:
	"""Refuse to undo when the source side has been cleaned up — reverting
	activity refs would point at deleted rows.
	"""
	for dt in ("Lead", "Opportunity", "Prospect"):
		if not frappe.db.exists("DocType", dt):
			continue
		if frappe.db.count(dt) > 0:
			return True
	return False


def _custom_field_exists(dt: str, fieldname: str) -> bool:
	return bool(frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}))


# ---------------------------------------------------------------------------
# whitelisted endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_undo_preview() -> dict:
	"""Return row counts that `undo_migration` would touch — powers the
	confirmation dialog. Does not perform any deletes / updates.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	all_locked, unlocked = _all_tabs_locked(settings)

	# Marker-tagged synthetics
	marker_counts: list[dict] = []
	for dt, marker in _MARKER_TAGGED_DELETES:
		if not frappe.db.exists("DocType", dt):
			continue
		if not _custom_field_exists(dt, marker):
			continue
		n = frappe.db.count(dt, {marker: ["is", "set"]})
		if n:
			marker_counts.append({"doctype": dt, "count": n, "marker": marker})

	# Open CRM-side ToDos synthesised by reshape_assignments
	crm_targets = list(REVERSE_DOCTYPE_MAP.values())
	synth_todos = (
		frappe.db.count("ToDo", {"reference_type": ["in", crm_targets], "status": "Open"})
		if frappe.db.exists("DocType", "ToDo")
		else 0
	)

	# Reshape-only target children
	reshape_children: list[dict] = []
	for dt, parent_types in _RESHAPE_TARGET_CHILDREN:
		if not frappe.db.exists("DocType", dt):
			continue
		n = frappe.db.count(dt, {"parenttype": ["in", parent_types]})
		if n:
			reshape_children.append({"doctype": dt, "count": n, "parenttypes": parent_types})

	# Reanchored shared-schema children — count rows whose parenttype the
	# runner flipped to a CRM target during migration.
	reanchored: list[dict] = []
	for child_dt, tgt_parent, src_parent in _REANCHORED_CHILDREN:
		if not frappe.db.exists("DocType", child_dt):
			continue
		n = frappe.db.count(child_dt, {"parenttype": tgt_parent})
		if n:
			reanchored.append({"doctype": child_dt, "count": n, "from": tgt_parent, "to": src_parent})

	# Dynamic Link repoint — Contact + Address links pointing at CRM targets
	dl_to_revert = 0
	if frappe.db.exists("DocType", "Dynamic Link"):
		dl_to_revert = frappe.db.count(
			"Dynamic Link",
			{
				"parenttype": ["in", ["Contact", "Address"]],
				"link_doctype": ["in", crm_targets],
			},
		)

	# Target parents
	parents: list[dict] = []
	for dt in _TARGET_PARENTS:
		if not frappe.db.exists("DocType", dt):
			continue
		n = frappe.db.count(dt)
		if n:
			parents.append({"doctype": dt, "count": n})

	return {
		"ok": True,
		"all_locked": all_locked,
		"unlocked_tabs": unlocked,
		"source_rows_remain": _source_rows_remain(),
		"marker_tagged": marker_counts,
		"synth_open_todos": synth_todos,
		"reshape_children": reshape_children,
		"reanchored": reanchored,
		"dynamic_link_repoints": dl_to_revert,
		"parents": parents,
	}


@frappe.whitelist()
def undo_migration(confirm: str = "") -> dict:
	"""Enqueue a background job that reverts the migration on the target side.

	Steps (executed in this order):
	  1. Revert activity references (CRM target → ERPNext source).
	  2. Revert Dynamic Link repoints on Contact/Address.
	  3. Revert re-anchored shared-schema children (parenttype back to source).
	  4. Delete marker-tagged synthetic rows (FCRM Note, CRM Task) + the
	     assignment ToDos those CRM Tasks own.
	  5. Delete open CRM-side assignment ToDos (synthesised from `_assign`).
	  6. Delete reshape-synthesised children on CRM Deal.
	  7. Delete CRM parent target rows (CRM Lead / CRM Deal / …).

	Guards: every tab locked + ERPNext source rows still exist + `confirm='DELETE'`.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)
	frappe.has_permission(RUN_DOCTYPE, "create", throw=True)

	if confirm != "DELETE":
		frappe.throw(
			_("Type DELETE to confirm this destructive operation."),
			title=_("Confirmation Missing"),
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	all_locked, unlocked = _all_tabs_locked(settings)
	if not all_locked:
		frappe.throw(
			_("Lock every tab before undoing the migration. Unlocked: {0}").format(", ".join(unlocked)),
			title=_("Tabs Not Locked"),
		)

	if not _source_rows_remain():
		frappe.throw(
			_(
				"ERPNext source rows have been cleaned up. Reverting activity references "
				"now would create orphan references. Undo is only safe before "
				"`Clean up ERPNext source data` runs."
			),
			title=_("Source Data Already Deleted"),
		)

	run = frappe.get_doc(
		{
			"doctype": RUN_DOCTYPE,
			"scope": "Single DocType",
			"scoped_to": None,
			"status": "Pending",
		}
	).insert()

	frappe.enqueue(
		"erpnext_crm_to_frappe_crm_migrator.api.undo._execute_undo_run",
		queue="long",
		timeout=60 * 60 * 2,
		run_name=run.name,
	)

	return {"ok": True, "run": run.name}


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


def _execute_undo_run(run_name: str) -> None:
	"""Background worker. One CRM Migration Run Step row per phase so the
	user sees live progress in /app/crm-migration-run.
	"""
	run = frappe.get_doc(RUN_DOCTYPE, run_name)
	run.status = "Running"
	run.started_at = now_datetime()
	run.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- live progress — show Undo run as Running before any step starts

	any_failed = False

	# Run Step's s_doctype / t_doctype columns are Link → DocType, so each
	# `_append_step` call has to pass a real doctype name. The user reads the
	# step's source/target columns to see which doctype that phase touched.

	# 1. Activity references — one step per activity doctype
	for activity_dt, result in activity.revert_all_activities():
		any_failed |= _append_step(run, activity_dt, result)

	# 2. Dynamic Link revert (Contact + Address)
	any_failed |= _append_step(run, "Dynamic Link", _revert_dynamic_links())

	# 3. Re-anchor revert — one step per shared-schema child doctype
	for child_dt, result in _revert_reanchored_children_per_doctype():
		any_failed |= _append_step(run, child_dt, result)

	# 4. Delete marker-tagged synthetics + assignment ToDos owned by deleted tasks
	any_failed |= _append_step(run, "FCRM Note", _delete_marker_tagged("FCRM Note", "custom_source_crm_note"))
	any_failed |= _append_step(run, "ToDo", _delete_assignment_todos_for_migrated_tasks())
	any_failed |= _append_step(run, "CRM Task", _delete_marker_tagged("CRM Task", "custom_source_todo"))

	# 5. Open CRM-side assignment ToDos synthesised from _assign cache
	any_failed |= _append_step(run, "ToDo", _delete_synth_open_todos())

	# 6. Reshape-synthesised children on CRM Deal
	for child_dt, parent_types in _RESHAPE_TARGET_CHILDREN:
		any_failed |= _append_step(run, child_dt, _delete_target_children(child_dt, parent_types))

	# 7. Parent target rows
	for parent_dt in _TARGET_PARENTS:
		any_failed |= _append_step(run, parent_dt, _delete_parent(parent_dt))

	run.completed_at = now_datetime()
	run.status = "Failed" if any_failed else "Succeeded"
	run.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- live progress — final Undo run status visible in the desk


# ---------------------------------------------------------------------------
# per-step helpers
# ---------------------------------------------------------------------------


def _append_step(run, doctype: str, result: dict) -> bool:
	"""Append a CRM Migration Run Step row and commit. Returns True if failed.

	`doctype` is written to both `s_doctype` and `t_doctype` (Link → DocType
	columns), so it must be an actual DocType name on the site.
	"""
	step = run.append(
		"steps",
		{
			"s_doctype": doctype,
			"t_doctype": doctype,
			"status": "Running",
			"started_at": now_datetime(),
		},
	)
	step.ok_count = result.get("ok", 0)
	step.skipped_count = result.get("skipped", 0)
	step.failed_count = result.get("failed", 0)
	if result.get("last_error"):
		step.last_error = result["last_error"][:500]
	if result.get("sample_failed"):
		step.sample_failed_names = ", ".join(result["sample_failed"])[:1000]

	failed = step.failed_count > 0
	if failed:
		step.status = "Failed"
	elif step.ok_count == 0 and step.skipped_count == 0:
		step.status = "Skipped"
	else:
		step.status = "Succeeded"
	step.completed_at = now_datetime()

	run.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- live progress — surface this step's status as it completes
	return failed


def _empty_result() -> dict:
	return {"ok": 0, "skipped": 0, "failed": 0, "last_error": "", "sample_failed": []}


def _revert_dynamic_links() -> dict:
	"""Flip link_doctype on Contact/Address Dynamic Links from CRM target
	back to ERPNext source.
	"""
	result = _empty_result()
	if not frappe.db.exists("DocType", "Dynamic Link"):
		return result

	dl = frappe.qb.DocType("Dynamic Link")
	for source_dt, target_dt in REVERSE_DOCTYPE_MAP.items():
		try:
			matched = frappe.db.count(
				"Dynamic Link",
				{
					"parenttype": ["in", ["Contact", "Address"]],
					"link_doctype": target_dt,
				},
			)
			if not matched:
				result["skipped"] += 1
				continue
			(
				frappe.qb.update(dl)
				.set(dl.link_doctype, source_dt)
				.where(dl.parenttype.isin(["Contact", "Address"]) & (dl.link_doctype == target_dt))
				.run()
			)
			result["ok"] += int(matched)
		except Exception as e:
			result["failed"] += 1
			result["last_error"] = f"{target_dt}: {e}"[:500]
			result["sample_failed"].append(f"{target_dt} → {source_dt}")
			frappe.log_error(title=f"Undo: Dynamic Link revert ({target_dt})", message=frappe.get_traceback())
	return result


def _revert_reanchored_children_per_doctype() -> list[tuple[str, dict]]:
	"""Flip parenttype back from CRM target to ERPNext source on
	shared-schema child tables, returning one (child_dt, result) per
	distinct child doctype so the worker can write one Run Step per row.
	"""
	by_doctype: dict[str, dict] = {}

	for child_dt, tgt_parent, src_parent in _REANCHORED_CHILDREN:
		result = by_doctype.setdefault(child_dt, _empty_result())
		if not frappe.db.exists("DocType", child_dt):
			result["skipped"] += 1
			continue
		try:
			matched = frappe.db.count(child_dt, {"parenttype": tgt_parent})
			if not matched:
				result["skipped"] += 1
				continue
			tbl = frappe.qb.DocType(child_dt)
			(frappe.qb.update(tbl).set(tbl.parenttype, src_parent).where(tbl.parenttype == tgt_parent).run())
			result["ok"] += int(matched)
		except Exception as e:
			result["failed"] += 1
			result["last_error"] = f"{child_dt} ({tgt_parent}→{src_parent}): {e}"[:500]
			result["sample_failed"].append(f"{child_dt}: {tgt_parent}→{src_parent}")
			frappe.log_error(
				title=f"Undo: reanchor revert ({child_dt})",
				message=frappe.get_traceback(),
			)

	return list(by_doctype.items())


def _delete_marker_tagged(target_dt: str, marker_field: str) -> dict:
	"""Delete every target_dt row where marker_field is set."""
	result = _empty_result()
	if not frappe.db.exists("DocType", target_dt):
		result["skipped"] += 1
		return result
	if not _custom_field_exists(target_dt, marker_field):
		result["skipped"] += 1
		return result
	try:
		n = frappe.db.count(target_dt, {marker_field: ["is", "set"]})
		if not n:
			result["skipped"] += 1
			return result
		frappe.db.delete(target_dt, {marker_field: ["is", "set"]})
		result["ok"] = n
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = str(e)[:500]
		frappe.log_error(title=f"Undo: marker delete {target_dt}", message=frappe.get_traceback())
	return result


def _delete_assignment_todos_for_migrated_tasks() -> dict:
	"""Delete assignment-style ToDos that reference migrated CRM Tasks.

	Those ToDos were synthesised right after the CRM Task bulk insert.
	We delete them BEFORE deleting the CRM Tasks themselves so the ToDo
	→ CRM Task references don't dangle even briefly.
	"""
	result = _empty_result()
	if not frappe.db.exists("DocType", "CRM Task") or not frappe.db.exists("DocType", "ToDo"):
		result["skipped"] += 1
		return result
	if not _custom_field_exists("CRM Task", "custom_source_todo"):
		result["skipped"] += 1
		return result
	try:
		ct = frappe.qb.DocType("CRM Task")
		task_names = [
			r[0]
			for r in frappe.qb.from_(ct).select(ct.name).where(ct["custom_source_todo"].isnotnull()).run()
		]
		if not task_names:
			result["skipped"] += 1
			return result

		n = frappe.db.count(
			"ToDo",
			{"reference_type": "CRM Task", "reference_name": ["in", task_names]},
		)
		if not n:
			result["skipped"] += 1
			return result

		frappe.db.delete(
			"ToDo",
			{"reference_type": "CRM Task", "reference_name": ["in", task_names]},
		)
		result["ok"] = n
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = str(e)[:500]
		frappe.log_error(title="Undo: CRM Task assignment ToDo delete", message=frappe.get_traceback())
	return result


def _delete_synth_open_todos() -> dict:
	"""Delete open ToDos whose reference_type is a CRM-side target — these
	are the assignment ToDos `reshape_assignments` synthesised from the
	`_assign` JSON cache.
	"""
	result = _empty_result()
	if not frappe.db.exists("DocType", "ToDo"):
		result["skipped"] += 1
		return result
	try:
		crm_targets = list(REVERSE_DOCTYPE_MAP.values())
		filters = {"reference_type": ["in", crm_targets], "status": "Open"}
		n = frappe.db.count("ToDo", filters)
		if not n:
			result["skipped"] += 1
			return result
		frappe.db.delete("ToDo", filters)
		result["ok"] = n
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = str(e)[:500]
		frappe.log_error(title="Undo: synth ToDo delete", message=frappe.get_traceback())
	return result


def _delete_target_children(child_dt: str, parent_types: list[str]) -> dict:
	"""Delete every child_dt row whose parenttype is in parent_types."""
	result = _empty_result()
	if not frappe.db.exists("DocType", child_dt):
		result["skipped"] += 1
		return result
	try:
		filters = {"parenttype": ["in", parent_types]}
		n = frappe.db.count(child_dt, filters)
		if not n:
			result["skipped"] += 1
			return result
		frappe.db.delete(child_dt, filters)
		result["ok"] = n
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = str(e)[:500]
		frappe.log_error(title=f"Undo: child delete {child_dt}", message=frappe.get_traceback())
	return result


def _delete_parent(parent_dt: str) -> dict:
	"""Delete every row of a CRM parent target doctype."""
	result = _empty_result()
	if not frappe.db.exists("DocType", parent_dt):
		result["skipped"] += 1
		return result
	try:
		n = frappe.db.count(parent_dt)
		if not n:
			result["skipped"] += 1
			return result
		frappe.db.delete(parent_dt)
		result["ok"] = n
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = str(e)[:500]
		frappe.log_error(title=f"Undo: parent delete {parent_dt}", message=frappe.get_traceback())
	return result

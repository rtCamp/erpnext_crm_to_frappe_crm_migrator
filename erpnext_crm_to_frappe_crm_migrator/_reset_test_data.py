"""Dev helper — reset CRM target data so a migration can be re-run.

Wipes every CRM Lead / Deal / Organization / lookup row + their
children, reverts the activity rewrite, drops the Run history.
Preserves the locked Field Map and per-tab Settings state. Source
ERPNext doctypes are NOT touched. See `docs/dev.md`. Dev/test only.
"""

import frappe

from erpnext_crm_to_frappe_crm_migrator.api.activity import ACTIVITY_SPECS
from erpnext_crm_to_frappe_crm_migrator.mapping.registry import REVERSE_DOCTYPE_MAP

# Ordered children-first so foreign-key-style constraints don't bite.
# (Frappe doesn't actually enforce FK, but ordering keeps logs clean.)
TARGET_PARENTS = [
	"CRM Lead",
	"CRM Deal",
	"CRM Organization",
	"CRM Territory",
	"CRM Industry",
	"CRM Lead Source",
	"CRM Lost Reason",
	"CRM Product",
]

# Child doctypes that live under the target parents.
TARGET_CHILDREN = [
	("CRM Products", ["CRM Deal"]),
	("CRM Contacts", ["CRM Deal"]),
	("CRM Status Change Log", ["CRM Lead", "CRM Deal"]),
	("CRM Rolling Response Time", ["CRM Lead", "CRM Deal"]),
]

# Reset target-side Dynamic Link rows pointing at CRM Organization
# (added by older reshape versions; the current reshape repoints in
# place rather than adding). Kept for compatibility with older runs.
DYNAMIC_LINK_TARGETS = ["CRM Organization"]


def reset_all():
	report: dict[str, int] = {}

	# 0. Revert the activity rewrite — flip reference_doctype
	# (and friends) back from CRM-side to ERPNext-side so the next
	# migration run has source-pointing rows to rewrite again. Done
	# before parent deletion so references stay resolvable to the
	# still-existing source rows during the rest of the cleanup.
	for activity_dt, doctype_field, _name_field in ACTIVITY_SPECS:
		if not frappe.db.exists("DocType", activity_dt):
			continue
		for source_dt, target_dt in REVERSE_DOCTYPE_MAP.items():
			# REVERSE_DOCTYPE_MAP keys are ERPNext source names;
			# values are CRM target names. We're rewriting target → source.
			n = frappe.db.count(activity_dt, {doctype_field: target_dt})
			if not n:
				continue
			frappe.db.sql(
				f"UPDATE `tab{activity_dt}` "
				f"SET `{doctype_field}` = %s "
				f"WHERE `{doctype_field}` = %s",
				(source_dt, target_dt),
			)
			report[f"activity {activity_dt}: {target_dt} → {source_dt}"] = n

	# 0b. Delete FCRM Notes that the notes reshape created — tagged
	# via the `custom_source_crm_note` marker the reshape installs at
	# runtime. User-created CRM-frontend FCRM Notes don't carry the
	# marker and are left alone.
	if (
		frappe.db.exists("DocType", "FCRM Note")
		and frappe.db.exists("Custom Field", {"dt": "FCRM Note", "fieldname": "custom_source_crm_note"})
	):
		n = frappe.db.count("FCRM Note", {"custom_source_crm_note": ["is", "set"]})
		if n:
			frappe.db.delete("FCRM Note", {"custom_source_crm_note": ["is", "set"]})
			report["FCRM Note (migrated)"] = n

	# 0c. Delete ToDos that the assignments reshape synthesised
	# from the _assign cache (those whose reference_type is a CRM
	# target). Re-running recreates them.
	if frappe.db.exists("DocType", "ToDo"):
		crm_targets = list(REVERSE_DOCTYPE_MAP.values())
		n = frappe.db.count(
			"ToDo",
			{"reference_type": ["in", crm_targets], "status": "Open"},
		)
		if n:
			frappe.db.delete(
				"ToDo",
				{"reference_type": ["in", crm_targets], "status": "Open"},
			)
			report["ToDo (CRM-side, Open)"] = n

	# 0d. Delete CRM Tasks that the tasks reshape created
	# (those with custom_source_todo set — tracks the source ERPNext
	# ToDo.name). Re-running recreates them. User-created CRM Tasks
	# don't carry this marker and are left alone.
	if (
		frappe.db.exists("DocType", "CRM Task")
		and frappe.db.exists("Custom Field", {"dt": "CRM Task", "fieldname": "custom_source_todo"})
	):
		n = frappe.db.count("CRM Task", {"custom_source_todo": ["is", "set"]})
		if n:
			frappe.db.delete("CRM Task", {"custom_source_todo": ["is", "set"]})
			report["CRM Task (migrated)"] = n

	# 1a. Re-anchored children (CRM Status Change Log) — these started life
	# on the source (parenttype='Lead'/'Opportunity'); the migrator's
	# _reanchor_shared_children flipped them to parenttype='CRM Lead' /
	# 'CRM Deal'. Reverting (instead of deleting) preserves the source
	# data so re-running the migration produces the same target state.
	# Migrator-synthesised rows (mig-stagelog-*) are deleted in step 1b
	# below — they're new copies, not source moves.
	if frappe.db.exists("DocType", "CRM Status Change Log"):
		for tgt_parent, src_parent in (
			("CRM Lead", "Lead"),
			("CRM Deal", "Opportunity"),
		):
			n = frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabCRM Status Change Log`
				WHERE parenttype = %s
				  AND parentfield = 'status_change_log'
				  AND name NOT LIKE 'mig-stagelog-%%'
				""",
				(tgt_parent,),
			)[0][0]
			if n:
				frappe.db.sql(
					"""
					UPDATE `tabCRM Status Change Log`
					SET parenttype = %s
					WHERE parenttype = %s
					  AND parentfield = 'status_change_log'
					  AND name NOT LIKE 'mig-stagelog-%%'
					""",
					(src_parent, tgt_parent),
				)
				report[f"reanchor revert CRM Status Change Log: {tgt_parent} → {src_parent}"] = int(n)

	# 1b. Delete synthesised child rows (mig-stagelog-* from the
	# Opportunity stage-log merge), then delete remaining target-side
	# child rows (CRM Products / CRM Contacts / CRM Rolling Response
	# Time — all reshape-synthesised, no source loss).
	if frappe.db.exists("DocType", "CRM Status Change Log"):
		n = frappe.db.count(
			"CRM Status Change Log",
			{"parenttype": ["in", ["CRM Lead", "CRM Deal"]], "name": ["like", "mig-stagelog-%"]},
		)
		if n:
			frappe.db.delete(
				"CRM Status Change Log",
				{"parenttype": ["in", ["CRM Lead", "CRM Deal"]], "name": ["like", "mig-stagelog-%"]},
			)
			report["child CRM Status Change Log (mig-stagelog)"] = n

	for child_dt, parent_types in TARGET_CHILDREN:
		if child_dt == "CRM Status Change Log":
			continue  # handled in 1a + 1b
		if not frappe.db.exists("DocType", child_dt):
			continue
		n_before = frappe.db.count(child_dt, {"parenttype": ["in", parent_types]})
		if n_before:
			frappe.db.delete(child_dt, {"parenttype": ["in", parent_types]})
			report[f"child {child_dt}"] = n_before

	# 2. Delete parent target rows.
	for parent_dt in TARGET_PARENTS:
		if not frappe.db.exists("DocType", parent_dt):
			continue
		n_before = frappe.db.count(parent_dt)
		if n_before:
			frappe.db.delete(parent_dt)
			report[parent_dt] = n_before

	# 3. Drop Dynamic Link rows added by older prospect-contacts reshape
	# versions (the current reshape repoints in place, not added).
	for link_doctype in DYNAMIC_LINK_TARGETS:
		n_before = frappe.db.count("Dynamic Link", {"link_doctype": link_doctype})
		if n_before:
			frappe.db.delete("Dynamic Link", {"link_doctype": link_doctype})
			report[f"Dynamic Link → {link_doctype}"] = n_before

	# 4. Run history only — keep CRM Migration Field Map intact and the
	# per-tab Settings state untouched so the user's mapping work
	# survives across reset cycles.
	for dt in ("CRM Migration Run Step", "CRM Migration Run"):
		if frappe.db.exists("DocType", dt):
			n_before = frappe.db.count(dt)
			if n_before:
				frappe.db.delete(dt)
				report[dt] = n_before

	frappe.db.commit()

	print("=== Reset complete ===")
	for k, v in sorted(report.items()):
		print(f"  {k:40s} {v}")
	print()
	print("Mapping config preserved. Tabs stay locked; CRM Migration Field Map")
	print("is intact. Next: /app/crm-migration-settings → Run Migration (or")
	print("Default: Skip All & Migrate to also (re-)refresh and re-lock).")
	return {"ok": True, "deleted": report}


def reset_deal_only():
	"""Same shape as `reset_all` but scoped to the CRM Deal pipeline only.

	Reverts CRM Deal activity refs, deletes Opportunity-originated migrated
	notes/tasks/child rows, and drops every CRM Deal row. CRM Lead /
	CRM Organization / lookup tables and their activity rewrites are NOT
	touched. Use to re-test the Opportunity → CRM Deal step in isolation.
	"""
	report: dict[str, int] = {}

	# 0. Revert activity refs CRM Deal → Opportunity (the only target/source
	# pair we care about for an Opportunity-scoped re-run).
	for activity_dt, doctype_field, _name_field in ACTIVITY_SPECS:
		if not frappe.db.exists("DocType", activity_dt):
			continue
		n = frappe.db.count(activity_dt, {doctype_field: "CRM Deal"})
		if not n:
			continue
		frappe.db.sql(
			f"UPDATE `tab{activity_dt}` SET `{doctype_field}` = %s WHERE `{doctype_field}` = %s",
			("Opportunity", "CRM Deal"),
		)
		report[f"activity {activity_dt}: CRM Deal → Opportunity"] = n

	# Also revert the ToDo assignment-only rewrite (CRM Deal → Opportunity).
	if frappe.db.exists("DocType", "ToDo"):
		n = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabToDo`
			WHERE reference_type = 'CRM Deal'
			  AND description LIKE 'Assignment for %%'
			""",
		)[0][0]
		if n:
			frappe.db.sql(
				"""
				UPDATE `tabToDo`
				SET reference_type = 'Opportunity'
				WHERE reference_type = 'CRM Deal'
				  AND description LIKE 'Assignment for %%'
				""",
			)
			report["activity ToDo (assignment): CRM Deal → Opportunity"] = int(n)

	# 0b. Migrated FCRM Notes whose parent is a CRM Deal — identified by
	# the `custom_source_crm_note` marker the reshape installs at runtime.
	if (
		frappe.db.exists("DocType", "FCRM Note")
		and frappe.db.exists("Custom Field", {"dt": "FCRM Note", "fieldname": "custom_source_crm_note"})
	):
		filters = {
			"custom_source_crm_note": ["is", "set"],
			"reference_doctype": "CRM Deal",
		}
		n = frappe.db.count("FCRM Note", filters)
		if n:
			frappe.db.delete("FCRM Note", filters)
			report["FCRM Note (CRM Deal, migrated)"] = n

	# 0c. Open CRM-side ToDos pointing at CRM Deal (auto-todos from CRM
	# Task.after_insert + reshape_assignments output).
	if frappe.db.exists("DocType", "ToDo"):
		n = frappe.db.count("ToDo", {"reference_type": "CRM Deal", "status": "Open"})
		if n:
			frappe.db.delete("ToDo", {"reference_type": "CRM Deal", "status": "Open"})
			report["ToDo (CRM Deal, Open)"] = n

	# 0d. Migrated CRM Tasks anchored to CRM Deal (or to Opportunity after
	# the activity revert above flipped them back). Match by marker field,
	# then narrow by either reference_doctype so we catch both states.
	if (
		frappe.db.exists("DocType", "CRM Task")
		and frappe.db.exists("Custom Field", {"dt": "CRM Task", "fieldname": "custom_source_todo"})
	):
		n = frappe.db.count(
			"CRM Task",
			{
				"custom_source_todo": ["is", "set"],
				"reference_doctype": ["in", ["CRM Deal", "Opportunity"]],
			},
		)
		if n:
			# Also drop the assignment ToDos those tasks own — the bulk
			# task reshape now synthesises one ToDo per assignee with
			# reference_type='CRM Task'. Re-running recreates them.
			task_names = frappe.db.sql_list(
				"""
				SELECT name FROM `tabCRM Task`
				WHERE `custom_source_todo` IS NOT NULL
				  AND reference_doctype IN ('CRM Deal', 'Opportunity')
				"""
			)
			if task_names:
				todo_n = frappe.db.count(
					"ToDo",
					{"reference_type": "CRM Task", "reference_name": ["in", task_names]},
				)
				if todo_n:
					frappe.db.delete(
						"ToDo",
						{"reference_type": "CRM Task", "reference_name": ["in", task_names]},
					)
					report["ToDo (assignment for migrated CRM Task)"] = todo_n
			frappe.db.delete(
				"CRM Task",
				{
					"custom_source_todo": ["is", "set"],
					"reference_doctype": ["in", ["CRM Deal", "Opportunity"]],
				},
			)
			report["CRM Task (CRM Deal, migrated)"] = n

	# 1a. Re-anchored children — revert (not delete). CRM Status Change Log
	# under CRM Deal started life as Opportunity.status_change_log rows
	# that the migrator's _reanchor_shared_children flipped over. Deleting
	# would destroy source data. Exclude mig-stagelog-* (those are newly
	# synthesised by reshape_opportunity_stage_logs — delete them in 1b).
	if frappe.db.exists("DocType", "CRM Status Change Log"):
		n = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabCRM Status Change Log`
			WHERE parenttype = 'CRM Deal'
			  AND parentfield = 'status_change_log'
			  AND name NOT LIKE 'mig-stagelog-%%'
			"""
		)[0][0]
		if n:
			frappe.db.sql(
				"""
				UPDATE `tabCRM Status Change Log`
				SET parenttype = 'Opportunity'
				WHERE parenttype = 'CRM Deal'
				  AND parentfield = 'status_change_log'
				  AND name NOT LIKE 'mig-stagelog-%%'
				"""
			)
			report["reanchor revert CRM Status Change Log: CRM Deal → Opportunity"] = int(n)

		# 1b. Drop only the merge-synthesised stage-log rows.
		n = frappe.db.count(
			"CRM Status Change Log",
			{"parenttype": "CRM Deal", "name": ["like", "mig-stagelog-%"]},
		)
		if n:
			frappe.db.delete(
				"CRM Status Change Log",
				{"parenttype": "CRM Deal", "name": ["like", "mig-stagelog-%"]},
			)
			report["child CRM Status Change Log (mig-stagelog)"] = n

	# 1c. Synthesised target-only children — safe to delete; nothing
	# upstream depends on them.
	for child_dt in ("CRM Products", "CRM Contacts", "CRM Rolling Response Time"):
		if not frappe.db.exists("DocType", child_dt):
			continue
		n = frappe.db.count(child_dt, {"parenttype": "CRM Deal"})
		if n:
			frappe.db.delete(child_dt, {"parenttype": "CRM Deal"})
			report[f"child {child_dt}"] = n

	# 2. CRM Deal parent rows.
	if frappe.db.exists("DocType", "CRM Deal"):
		n = frappe.db.count("CRM Deal")
		if n:
			frappe.db.delete("CRM Deal")
			report["CRM Deal"] = n

	# 3. Migration Run history scoped to Opportunity (so the next run is
	# the only one in the table, easier to read).
	if frappe.db.exists("DocType", "CRM Migration Run"):
		runs = frappe.db.sql_list(
			"SELECT name FROM `tabCRM Migration Run` WHERE scoped_to = 'Opportunity'"
		)
		for r in runs:
			frappe.db.delete("CRM Migration Run Step", {"parent": r})
			frappe.db.delete("CRM Migration Run", {"name": r})
		if runs:
			report["CRM Migration Run (Opportunity)"] = len(runs)

	frappe.db.commit()

	print("=== Deal-only reset complete ===")
	for k, v in sorted(report.items()):
		print(f"  {k:40s} {v}")
	return {"ok": True, "deleted": report}

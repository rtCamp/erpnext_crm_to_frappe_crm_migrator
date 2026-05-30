"""Dev helper — synchronously run the UTM Source migration step. See `docs/dev.md`."""

import frappe

from erpnext_crm_to_frappe_crm_migrator.api.runner import _execute_run


def run_inline():
	# Reset CRM Lead Source so we see fresh inserts.
	if frappe.db.exists("DocType", "CRM Lead Source"):
		n = frappe.db.count("CRM Lead Source")
		if n:
			frappe.db.delete("CRM Lead Source")
			print(f"Cleared {n} CRM Lead Source rows.")

	run = frappe.get_doc(
		{
			"doctype": "CRM Migration Run",
			"scope": "Single DocType",
			"scoped_to": "UTM Source",
			"status": "Pending",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	print(f"Created run: {run.name}")
	_execute_run(run_name=run.name, source_doctype="UTM Source")

	run.reload()
	for step in run.steps:
		print(
			f"step: {step.s_doctype} → {step.t_doctype}  status={step.status}  "
			f"ok={step.ok_count} skipped={step.skipped_count} failed={step.failed_count}"
		)
		if step.last_error:
			print(f"  last_error: {step.last_error}")

	# Inspect a few rows.
	rows = frappe.db.sql(
		"""
		SELECT name, source_name, details
		FROM `tabCRM Lead Source`
		LIMIT 5
		""",
		as_dict=True,
	)
	print("\nSample CRM Lead Source rows:")
	for r in rows:
		print(f"  name={r['name']!r}  source_name={r['source_name']!r}  details={(r['details'] or '')[:50]!r}")

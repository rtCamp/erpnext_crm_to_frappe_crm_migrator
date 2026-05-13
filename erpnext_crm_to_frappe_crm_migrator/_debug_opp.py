"""TEMP — synchronously run the Opportunity migration step and print errors.

Run via:
    bench --site crm.localhost execute \
        erpnext_crm_to_frappe_crm_migrator._debug_opp.run_inline

Calls the orchestrator inline (skipping enqueue), then prints the resulting
Run record + recent Error Log entries the migrator wrote.
"""

import frappe

from erpnext_crm_to_frappe_crm_migrator.api.runner import _execute_run


def run_inline():
	run = frappe.get_doc(
		{
			"doctype": "CRM Migration Run",
			"scope": "Single DocType",
			"scoped_to": "Opportunity",
			"status": "Pending",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	print(f"Created run: {run.name}")
	print("Executing inline (this can take a few minutes for 2k+ opportunities)…")
	_execute_run(run_name=run.name, source_doctype="Opportunity")

	run.reload()
	print()
	print(f"=== Run {run.name} → {run.status} ===")
	print(f"started_at:   {run.started_at}")
	print(f"completed_at: {run.completed_at}")
	print()
	for step in run.steps:
		print(
			f"step: {step.s_doctype} → {step.t_doctype}  "
			f"status={step.status}  "
			f"ok={step.ok_count} skipped={step.skipped_count} failed={step.failed_count}  "
			f"child_ok={step.child_ok_count or 0} child_failed={step.child_failed_count or 0} "
			f"reanchor={step.child_reanchor_count or 0}"
		)
		if step.last_error:
			print(f"  last_error: {step.last_error}")
		if step.sample_failed_names:
			print(f"  sample_failed: {step.sample_failed_names}")

	# Error Log entries from this run window.
	errs = frappe.db.sql(
		"""
		SELECT name, method, creation, LEFT(error, 800) AS error
		FROM `tabError Log`
		WHERE creation >= %s
		  AND method LIKE 'Migrator%%'
		ORDER BY creation DESC
		LIMIT 25
		""",
		(run.started_at,),
		as_dict=True,
	)
	print()
	print(f"=== Error Log entries since {run.started_at} ===")
	if not errs:
		print("  (none)")
	for e in errs:
		print(f"\n--- {e['creation']}  {e['method']}  ({e['name']})")
		print(e["error"])

	return {"run": run.name, "status": run.status, "errors": len(errs)}

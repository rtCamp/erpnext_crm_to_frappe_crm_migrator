"""TEMP — reset all CRM target data + migrator state for re-running migration.

Run via:
    bench --site crm.localhost execute \\
        erpnext_crm_to_frappe_crm_migrator._reset_test_data.reset_all

DESTRUCTIVE on the target site — wipes every CRM Lead, CRM Deal,
CRM Organization, CRM Territory, CRM Industry, CRM Lead Source,
CRM Lost Reason, CRM Product, plus their child rows, plus the
migrator's locked Field Map and Run history. Source ERPNext doctypes
(Lead/Opportunity/Prospect/etc.) are NOT touched.

Intended for dev/test cycles only — DO NOT run on production.
"""

import frappe

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

# Phase 3 Prospect-contacts reshape adds Dynamic Link rows on Contact
# pointing at CRM Organization. Reset those so the reshape can re-run cleanly.
DYNAMIC_LINK_TARGETS = ["CRM Organization"]


def reset_all():
	report: dict[str, int] = {}

	# 1. Delete child-table rows belonging to target parents.
	for child_dt, parent_types in TARGET_CHILDREN:
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

	# 3. Drop Dynamic Link rows added by Phase 3 prospect-contacts reshape.
	for link_doctype in DYNAMIC_LINK_TARGETS:
		n_before = frappe.db.count("Dynamic Link", {"link_doctype": link_doctype})
		if n_before:
			frappe.db.delete("Dynamic Link", {"link_doctype": link_doctype})
			report[f"Dynamic Link → {link_doctype}"] = n_before

	# 4. Reset migrator state — Field Map, Run log, per-tab locks.
	for dt in ("CRM Migration Field Map", "CRM Migration Run Step", "CRM Migration Run"):
		if frappe.db.exists("DocType", dt):
			n_before = frappe.db.count(dt)
			if n_before:
				frappe.db.delete(dt)
				report[dt] = n_before

	# Unlock every tab on the Settings doc + clear its mapped data.
	from erpnext_crm_to_frappe_crm_migrator.mapping.registry import SOURCE_DOCTYPES
	settings = frappe.get_single("CRM Migration Settings")
	for s in SOURCE_DOCTYPES:
		prefix = s.lower().replace(" ", "_")
		settings.set(f"{prefix}_locked", 0)
		settings.set(f"{prefix}_locked_on", None)
		settings.set(f"{prefix}_field_mapping", [])
		settings.set(f"{prefix}_mapped_meta", "[]")
		settings.set(f"{prefix}_count", 0)
	settings.save(ignore_permissions=True)
	report["Settings tabs reset"] = len(SOURCE_DOCTYPES)

	frappe.db.commit()

	print("=== Reset complete ===")
	for k, v in sorted(report.items()):
		print(f"  {k:40s} {v}")
	print()
	print("Next: re-run /app/crm-migration-settings → Refresh Diff → Default: Skip All & Migrate")
	return {"ok": True, "deleted": report}

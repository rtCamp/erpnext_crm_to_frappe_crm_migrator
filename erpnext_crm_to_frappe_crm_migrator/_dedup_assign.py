"""Dev helper — retroactive dedup of `_assign` / `_user_tags` / `_liked_by`
JSON lists on already-migrated CRM target rows. Idempotent. See
`docs/dev.md`. Dev/test only.
"""

import json

import frappe

_FIELDS = ("_assign", "_user_tags", "_liked_by")
_DOCTYPES = (
	"CRM Lead",
	"CRM Deal",
	"CRM Organization",
	"CRM Territory",
	"CRM Industry",
	"CRM Lead Source",
	"CRM Lost Reason",
	"CRM Product",
)


def _dedup(raw):
	if not raw or not isinstance(raw, str):
		return raw
	try:
		decoded = json.loads(raw)
	except (json.JSONDecodeError, TypeError):
		return raw
	if not isinstance(decoded, list):
		return raw
	seen: set = set()
	deduped: list = []
	for item in decoded:
		key = item if isinstance(item, (str, int, float, bool)) else json.dumps(item, sort_keys=True)
		if key in seen:
			continue
		seen.add(key)
		deduped.append(item)
	return json.dumps(deduped) if len(deduped) != len(decoded) else raw


def run():
	report: dict[str, int] = {}
	for dt in _DOCTYPES:
		if not frappe.db.exists("DocType", dt):
			continue
		tbl = frappe.qb.DocType(dt)
		for fld in _FIELDS:
			col = tbl[fld]
			rows = (
				frappe.qb.from_(tbl)
				.select(tbl.name, col)
				.where(col.isnotnull() & (col != "") & (col != "[]"))
			).run(as_dict=True)
			updated = 0
			for r in rows:
				deduped = _dedup(r[fld])
				if deduped != r[fld]:
					(frappe.qb.update(tbl).set(col, deduped).where(tbl.name == r["name"]).run())
					updated += 1
			if updated:
				report[f"{dt}.{fld}"] = updated

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- dev helper — persist per-row dedup UPDATEs before printing the report
	print("=== Dedup complete ===")
	if not report:
		print("  (no duplicates found)")
	for k, v in sorted(report.items()):
		print(f"  {k:40s} {v}")
	return {"ok": True, "updated": report}

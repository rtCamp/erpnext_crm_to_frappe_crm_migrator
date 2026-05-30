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
		key = (
			item
			if isinstance(item, (str, int, float, bool))
			else json.dumps(item, sort_keys=True)
		)
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
		for fld in _FIELDS:
			rows = frappe.db.sql(
				f"""
				SELECT name, `{fld}`
				FROM `tab{dt}`
				WHERE `{fld}` IS NOT NULL AND `{fld}` != '' AND `{fld}` != '[]'
				""",
				as_dict=True,
			)
			updated = 0
			for r in rows:
				deduped = _dedup(r[fld])
				if deduped != r[fld]:
					frappe.db.sql(
						f"UPDATE `tab{dt}` SET `{fld}` = %s WHERE name = %s",
						(deduped, r["name"]),
					)
					updated += 1
			if updated:
				report[f"{dt}.{fld}"] = updated

	frappe.db.commit()
	print("=== Dedup complete ===")
	if not report:
		print("  (no duplicates found)")
	for k, v in sorted(report.items()):
		print(f"  {k:40s} {v}")
	return {"ok": True, "updated": report}

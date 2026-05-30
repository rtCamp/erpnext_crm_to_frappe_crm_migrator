"""Whitelisted endpoints powering the CRM Migration Settings UI.

`refresh_diff` rebuilds the per-tab diff; `lock_doctype` / `unlock_doctype`
control per-tab freeze state. See `docs/mapping.md` for the resolution
order and routing mechanisms.
"""

from __future__ import annotations

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_crm_to_frappe_crm_migrator.mapping.registry import (
	ALL_SKIP_FIELDS,
	RESHAPE_HANDLED_FIELDS,
	REVERSE_DOCTYPE_MAP,
	SOURCE_DOCTYPES,
	SUGGESTION_MAP,
)

SETTINGS_DOCTYPE = "CRM Migration Settings"
FIELD_MAP_DOCTYPE = "CRM Migration Field Map"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _settings_field_prefix(source_doctype: str) -> str:
	"""Snake_case slug used in CRM Migration Settings field names.

	"Industry Type" -> "industry_type", "Lead" -> "lead",
	"Opportunity Lost Reason" -> "opportunity_lost_reason".
	"""
	return source_doctype.lower().replace(" ", "_")


def _doctype_exists(doctype: str) -> bool:
	return frappe.db.exists("DocType", doctype) is not None


def _safe_get_meta(doctype: str):
	"""Return frappe.get_meta or None if the doctype isn't installed."""
	if not _doctype_exists(doctype):
		return None
	return frappe.get_meta(doctype)


def _types_compatible(src_df, tgt_df) -> tuple[bool, str]:
	"""Return (compatible, risk_note).

	"Compatible" here means "same fieldtype, plus, for Link/Table fields,
	same options". Anything else gets a risk note so the user can flip
	the row to Skip if they want.
	"""
	if src_df.fieldtype != tgt_df.fieldtype:
		return False, (
			f"type mismatch: {src_df.fieldtype} → {tgt_df.fieldtype}"
		)

	if src_df.fieldtype == "Link":
		if (src_df.options or "") != (tgt_df.options or ""):
			return False, (
				f"Link options differ: {src_df.options or '?'} "
				f"→ {tgt_df.options or '?'}"
			)

	if src_df.fieldtype in ("Table", "Table MultiSelect"):
		if (src_df.options or "") != (tgt_df.options or ""):
			return False, (
				f"child table type differs: {src_df.options or '?'} "
				f"→ {tgt_df.options or '?'}"
			)

	return True, ""


# Dynamic-Link routing rules. Keyed by (source_doctype, source_field).
# Each rule's `routes` maps controller-field values (e.g. opportunity_from
# == "Lead") to the target CRM Deal column to write into.
DYNAMIC_LINK_ROUTES: dict[tuple[str, str], dict] = {
	("Opportunity", "party_name"): {
		"controller_field": "opportunity_from",
		# Map: controller value → target column on CRM Deal
		"routes": {
			"Lead": "lead",
			"Prospect": "organization",
		},
		# Used when controller value isn't in `routes` (e.g. "Customer"
		# or empty) — falls back to the org column for compatibility
		# with the historic registry suggestion.
		"default_target": "organization",
	},
}


def _dynamic_link_note(src_df, source_doctype: str) -> dict | None:
	"""If this source field is a Dynamic Link covered by a known routing
	rule, return the rule plus a human-readable risk note for the diff.
	Otherwise None.
	"""
	if src_df.fieldtype != "Dynamic Link":
		return None
	rule = DYNAMIC_LINK_ROUTES.get((source_doctype, src_df.fieldname))
	if not rule:
		return None
	pretty = ", ".join(f"{k} → {v}" for k, v in rule["routes"].items())
	risk = (
		f"Dynamic Link — runner routes value to a different target "
		f"column based on '{rule['controller_field']}' ({pretty}). "
		f"Default fallback: {rule['default_target']}."
	)
	return {**rule, "risk": risk}


def _is_layout_field(df) -> bool:
	"""Section/Column/Tab breaks, HTML, Heading, Button — never user data."""
	return df.fieldtype in (
		"Section Break",
		"Column Break",
		"Tab Break",
		"HTML",
		"HTML Editor",
		"Heading",
		"Button",
		"Fold",
	)


def _build_row_for_field(src_df, source_doctype: str, target_doctype: str, suggestions: dict) -> dict:
	"""Compute a single CRM Migration Field Row dict from a source DocField.

	Resolution order:
	1. Registry hit → action=Map, target_field from registry.
	2. Exact same-name match on target meta → action=Map; risk only if
	   the fieldtypes/Link options differ.
	3. Registry hit but the named target field isn't on target meta →
	   blank action with an explanatory risk (user picks Map or Skip).
	4. No candidate at all → blank action with a "no candidate" risk.

	Blank action ("") is the "needs decision" state — lock_doctype
	refuses until every row has either Map or Skip.

	Dynamic-Link source fields (e.g. Opportunity.party_name) get a
	registry-style mapping plus a runtime-override risk note — the
	core records runner reroutes the value at write time based on the
	companion controller field (e.g. opportunity_from).
	"""
	target_meta = _safe_get_meta(target_doctype)
	src_field = src_df.fieldname

	row = {
		"source_field": src_field,
		"source_label": src_df.label or src_field,
		"source_type": src_df.fieldtype,
		"is_custom": 1 if getattr(src_df, "is_custom_field", 0) else 0,
		"suggested_target": "",
		"target_field": "",
		"action": "",
		"risk": "",
	}

	# Dynamic-Link fields can't be statically mapped to a single target
	# column — the runner picks the right column at write time. We
	# still surface a primary mapping (so the row counts as Map) plus
	# a risk note explaining the dynamic routing.
	dynamic_note = _dynamic_link_note(src_df, source_doctype)
	if dynamic_note:
		# default the target to the registry hit if any, otherwise the first
		# column listed in the dynamic note's `routes`.
		default_target = suggestions.get(src_field) or dynamic_note["default_target"]
		row["suggested_target"] = default_target
		row["target_field"] = default_target
		row["action"] = "Map"
		row["risk"] = dynamic_note["risk"]
		return row

	# (1) registry hit
	registry_target = suggestions.get(src_field)
	if registry_target:
		row["suggested_target"] = registry_target

		tgt_df = target_meta.get_field(registry_target) if target_meta else None
		if tgt_df is None:
			row["action"] = ""
			row["risk"] = (
				f"registry target '{registry_target}' missing on "
				f"{target_doctype} meta"
			)
			return row

		row["target_field"] = registry_target
		row["action"] = "Map"
		ok, note = _types_compatible(src_df, tgt_df)
		if not ok:
			row["risk"] = note
		return row

	# (2) exact same-name match on target meta
	if target_meta is not None:
		tgt_df = target_meta.get_field(src_field)
		if tgt_df is not None:
			row["suggested_target"] = src_field
			row["target_field"] = src_field
			row["action"] = "Map"
			ok, note = _types_compatible(src_df, tgt_df)
			if not ok:
				row["risk"] = note
			return row

	# (3) no candidate
	row["risk"] = f"no candidate in {target_doctype}"
	return row


def _build_rows_for_source(source_doctype: str) -> list[dict]:
	target_doctype = REVERSE_DOCTYPE_MAP.get(source_doctype, "")
	if not target_doctype:
		return []

	src_meta = _safe_get_meta(source_doctype)
	if src_meta is None:
		return []

	suggestions = SUGGESTION_MAP.get(source_doctype, {})
	reshape_handled = RESHAPE_HANDLED_FIELDS.get(source_doctype, set())
	rows = []
	for df in src_meta.fields:
		if _is_layout_field(df):
			continue
		if df.fieldname in ALL_SKIP_FIELDS:
			continue
		# Table / Table MultiSelect fields can't be column-mapped to a
		# single target field. The core runner's
		# `_reanchor_shared_children` moves rows for same-schema Tables
		# (e.g. status_change_log) directly from source meta; reshape
		# functions handle structural mismatches (Opportunity Item →
		# CRM Products, lost_reasons → lost_reason). Hiding Tables here
		# keeps the diff focused on real column mappings.
		if df.fieldtype in ("Table", "Table MultiSelect"):
			continue
		# Source fields whose data lands on the target via a reshape
		# (e.g. Opportunity.order_lost_reason → CRM Deal.lost_notes
		# via reshape_opportunity_lost_reasons) — hide so the user
		# isn't asked to resolve a row that's already covered.
		if df.fieldname in reshape_handled:
			continue
		rows.append(_build_row_for_field(df, source_doctype, target_doctype, suggestions))

	# Two source fields cannot legitimately write to the same target
	# column — last-writer-wins would silently lose data. When a target
	# is claimed by more than one row, prefer the row that was mapped
	# via the registry over a row that came from same-name fallback;
	# the registry encodes intentional cross-system rename rules and
	# should always win over an accidental fieldname collision.
	#
	# Dynamic Link source fields (e.g. Opportunity.party_name) are
	# excluded from this check — they're runtime-routed by the runner
	# and only write to their nominal target some of the time, so
	# they don't actually block another source from also claiming the
	# column.
	target_to_candidates: dict[str, list[dict]] = defaultdict(list)
	for row in rows:
		if row["action"] != "Map" or not row["target_field"]:
			continue
		if (source_doctype, row["source_field"]) in DYNAMIC_LINK_ROUTES:
			continue
		target_to_candidates[row["target_field"]].append(row)

	for target, candidates in target_to_candidates.items():
		if len(candidates) <= 1:
			continue
		registry_winners = [r for r in candidates if r["source_field"] in suggestions]
		winner = registry_winners[0] if registry_winners else candidates[0]
		for loser in candidates:
			if loser is winner:
				continue
			loser["action"] = ""
			loser["risk"] = (
				f"duplicate target — '{target}' is already claimed "
				f"by source field '{winner['source_field']}'. Map only one."
			)
			loser["target_field"] = ""
	return rows


# ---------------------------------------------------------------------------
# whitelisted endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist()
def refresh_diff() -> dict:
	"""Rebuild per-tab data for every UNLOCKED source. Locked tabs are
	preserved untouched so the user's resolved state survives.

	Returns a per-source-doctype summary; locked tabs appear with
	`{"locked": true}` so the UI can surface that they were skipped.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)

	settings = frappe.get_single(SETTINGS_DOCTYPE)

	summary = {}
	for source_doctype in SOURCE_DOCTYPES:
		prefix = _settings_field_prefix(source_doctype)
		table_field = f"{prefix}_field_mapping"
		count_field = f"{prefix}_count"
		target_field = f"target_{prefix}"
		mapped_meta_field = f"{prefix}_mapped_meta"
		locked_field = f"{prefix}_locked"

		# Always keep the target label fresh — even on locked tabs it's
		# useful context.
		settings.set(target_field, REVERSE_DOCTYPE_MAP.get(source_doctype, ""))

		if settings.get(locked_field):
			summary[source_doctype] = {"locked": True}
			continue

		# Source not installed on this site (e.g. customer doesn't use Item) —
		# leave the table empty and report 0 rows.
		if not _doctype_exists(source_doctype):
			settings.set(table_field, [])
			settings.set(mapped_meta_field, "[]")
			settings.set(count_field, 0)
			summary[source_doctype] = {"rows": 0, "skipped": True}
			continue

		rows = _build_rows_for_source(source_doctype)

		# Split: auto-mapped rows go to JSON (rendered as HTML summary by
		# the client); rows needing user attention land in the editable
		# table.
		mapped_rows = [r for r in rows if r["action"] == "Map"]
		actionable_rows = [r for r in rows if r["action"] != "Map"]

		settings.set(table_field, actionable_rows)
		# Trim mapped rows to display-only fields before serialising — keeps
		# the JSON small and avoids accidentally exposing internal fields.
		display_meta = [
			{
				"source_field": r["source_field"],
				"source_label": r["source_label"],
				"source_type": r["source_type"],
				"is_custom": r["is_custom"],
				"target_field": r["target_field"],
				"risk": r["risk"],
			}
			for r in mapped_rows
		]
		settings.set(mapped_meta_field, json.dumps(display_meta))

		try:
			row_count = frappe.db.count(source_doctype)
		except Exception:
			row_count = 0
		settings.set(count_field, row_count)

		bucket = {"map": 0, "unresolved": 0, "with_risk": 0}
		for row in rows:
			if row["action"] == "Map":
				bucket["map"] += 1
				if row["risk"]:
					bucket["with_risk"] += 1
			elif not row["action"]:
				# Blank action = needs decision (formerly "Unresolved")
				bucket["unresolved"] += 1
		summary[source_doctype] = {
			"rows": len(rows),
			"row_count": row_count,
			**bucket,
		}

	settings.flags.ignore_permissions = False
	settings.save()

	return {"ok": True, "summary": summary}


@frappe.whitelist()
def lock_doctype(source_doctype: str) -> dict:
	"""Freeze action=Map rows for ONE source into CRM Migration Field Map.

	Refuses if the named tab still has rows with a blank Action. Replaces any
	previously-locked Field Map rows for this source so re-locks are
	idempotent.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)
	frappe.has_permission(FIELD_MAP_DOCTYPE, "create", throw=True)

	if source_doctype not in SOURCE_DOCTYPES:
		frappe.throw(
			_("Unknown source doctype: {0}").format(source_doctype),
			title=_("Bad Argument"),
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	prefix = _settings_field_prefix(source_doctype)

	# 1a. Validate — refuse if any row still has a blank action (i.e.
	# the user hasn't chosen Map or Skip for it).
	rows = settings.get(f"{prefix}_field_mapping") or []
	unresolved = [r for r in rows if not r.action]
	if unresolved:
		fields = ", ".join(r.source_field for r in unresolved)
		frappe.throw(
			_("Set action (Map or Skip) on these {0} fields before locking {1}: {2}").format(
				len(unresolved), source_doctype, fields,
			),
			title=_("Action Required"),
		)

	# 1b. Validate — two source fields can't write to the same target.
	# Walk both the JSON-side mapped rows and the table-side Map rows
	# and refuse if any target appears more than once. Dynamic Link
	# source fields are runtime-routed (they don't always write to
	# their nominal target) and are excluded from this check.
	target_to_sources: dict[str, list[str]] = {}
	raw_meta = settings.get(f"{prefix}_mapped_meta") or "[]"
	try:
		mapped_meta_preview = json.loads(raw_meta)
	except json.JSONDecodeError:
		mapped_meta_preview = []
	for entry in mapped_meta_preview:
		src = entry.get("source_field")
		tgt = entry.get("target_field")
		if not src or not tgt:
			continue
		if (source_doctype, src) in DYNAMIC_LINK_ROUTES:
			continue
		target_to_sources.setdefault(tgt, []).append(src)
	for row in rows:
		if row.action != "Map" or not row.target_field:
			continue
		if (source_doctype, row.source_field) in DYNAMIC_LINK_ROUTES:
			continue
		target_to_sources.setdefault(row.target_field, []).append(row.source_field)
	conflicts = {tgt: srcs for tgt, srcs in target_to_sources.items() if len(srcs) > 1}
	if conflicts:
		lines = [
			f"<b>{tgt}</b> ← {', '.join(srcs)}"
			for tgt, srcs in conflicts.items()
		]
		frappe.throw(
			_("Two source fields cannot write to the same target column. Pick one per target:")
			+ "<br>" + "<br>".join(lines),
			title=_("Duplicate Target Mapping"),
		)

	# 2. Wipe previous lock for this source — fresh re-lock.
	frappe.db.delete(FIELD_MAP_DOCTYPE, {"s_doctype": source_doctype})

	# 3. Insert one row per mapped pair, combining JSON (auto) + table (manual).
	target_doctype = REVERSE_DOCTYPE_MAP.get(source_doctype, "")
	if not target_doctype:
		frappe.throw(
			_("No target mapping for {0}").format(source_doctype),
			title=_("Bad State"),
		)

	locked_on = now_datetime()
	inserted = 0
	seen: set[str] = set()

	raw_meta = settings.get(f"{prefix}_mapped_meta") or "[]"
	try:
		mapped_meta = json.loads(raw_meta)
	except json.JSONDecodeError:
		mapped_meta = []
	for entry in mapped_meta:
		src = entry.get("source_field")
		tgt = entry.get("target_field")
		if not src or not tgt or src in seen:
			continue
		seen.add(src)
		frappe.get_doc(
			{
				"doctype": FIELD_MAP_DOCTYPE,
				"s_doctype": source_doctype,
				"t_doctype": target_doctype,
				"source": src,
				"target": tgt,
				"locked_on": locked_on,
			}
		).insert(ignore_permissions=False)
		inserted += 1

	for row in rows:
		if row.action != "Map":
			continue
		if not row.target_field or row.source_field in seen:
			continue
		seen.add(row.source_field)
		frappe.get_doc(
			{
				"doctype": FIELD_MAP_DOCTYPE,
				"s_doctype": source_doctype,
				"t_doctype": target_doctype,
				"source": row.source_field,
				"target": row.target_field,
				"locked_on": locked_on,
			}
		).insert(ignore_permissions=False)
		inserted += 1

	settings.set(f"{prefix}_locked", 1)
	settings.set(f"{prefix}_locked_on", locked_on)
	settings.save()

	return {"ok": True, "source": source_doctype, "inserted": inserted}


@frappe.whitelist()
def get_target_doctype_fields() -> dict:
	"""Return {crm_target_doctype: [fieldname, ...]} for the 8 known target
	doctypes — used by the client script to populate the Autocomplete
	options on each tab's `target_field` column.
	"""
	out: dict[str, list[str]] = {}
	for source_dt in SOURCE_DOCTYPES:
		target_dt = REVERSE_DOCTYPE_MAP.get(source_dt)
		if not target_dt or not _doctype_exists(target_dt):
			continue
		meta = frappe.get_meta(target_dt)
		fields = []
		for df in meta.fields:
			if not df.fieldname:
				continue
			if _is_layout_field(df):
				continue
			fields.append(df.fieldname)
		out[target_dt] = sorted(set(fields))
	return out


@frappe.whitelist()
def mark_all_as_skip(source_doctype: str) -> dict:
	"""Set every blank-action row in one tab's editable table to Skip,
	persisting the change server-side.

	Why server-side: the Settings form has `disable_save()` so client
	mutations don't have a save path. This endpoint mutates and saves
	in one round-trip, then the client reloads.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)
	if source_doctype not in SOURCE_DOCTYPES:
		frappe.throw(
			_("Unknown source doctype: {0}").format(source_doctype),
			title=_("Bad Argument"),
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	prefix = _settings_field_prefix(source_doctype)
	if settings.get(f"{prefix}_locked"):
		frappe.throw(
			_("{0} is locked. Unlock it before bulk-skipping rows.").format(source_doctype),
			title=_("Tab Locked"),
		)

	rows = settings.get(f"{prefix}_field_mapping") or []
	touched = 0
	for row in rows:
		if row.action != "Skip":
			row.action = "Skip"
			row.target_field = ""
			row.risk = ""
			touched += 1
	settings.save()
	return {"ok": True, "source": source_doctype, "touched": touched}


@frappe.whitelist()
def unlock_doctype(source_doctype: str) -> dict:
	"""Clear the lock flag on ONE tab.

	Does NOT delete the CRM Migration Field Map rows for this source —
	any in-flight migration job that's reading them stays consistent.
	The next `lock_doctype` will replace them.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "write", throw=True)
	if source_doctype not in SOURCE_DOCTYPES:
		frappe.throw(
			_("Unknown source doctype: {0}").format(source_doctype),
			title=_("Bad Argument"),
		)

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	prefix = _settings_field_prefix(source_doctype)
	settings.set(f"{prefix}_locked", 0)
	settings.set(f"{prefix}_locked_on", None)
	settings.save()
	return {"ok": True, "source": source_doctype}


@frappe.whitelist()
def get_migrator_details() -> dict:
	"""Return the activity doctypes the migrator rewrites.

	Powers the Details tab on CRM Migration Settings — gives the user a
	single reference for which audit-trail / activity records get re-pointed
	at the migrated CRM doctypes after the core records migration runs.

	ToDo is appended as a synthetic entry because its rewrite is narrowed
	(only assignment-style ToDos), so it doesn't live in ACTIVITY_SPECS but
	is part of what the activity step does.
	"""
	from erpnext_crm_to_frappe_crm_migrator.api.activity import ACTIVITY_SPECS

	rows = [
		{"doctype": dt, "field": field_dt, "scope": ""}
		for (dt, field_dt, _nf) in ACTIVITY_SPECS
	]
	rows.append({
		"doctype": "ToDo",
		"field": "reference_type",
		"scope": "assignment-style rows only — content ToDos become CRM Tasks instead",
	})
	return {"activity_doctypes": rows}

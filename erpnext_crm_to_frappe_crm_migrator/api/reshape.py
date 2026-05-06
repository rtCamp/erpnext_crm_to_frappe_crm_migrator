"""Phase 3 — schema reshape between source and target child tables.

The Phase-2 runner skips Table-type fields entirely; Phase 3 fills them
in for the three reshape pairs that need a structural change:

  1. Prospect contacts → CRM Organization Contact.links (Dynamic Link).
  2. Opportunity Items (Item refs) → CRM Products child rows, with
     auto-create of any missing CRM Product on demand.
  3. Opportunity contact_person + Contact.links → CRM Deal.contacts
     child rows (multi-contact when ERPNext Contacts are linked to the
     Prospect via Dynamic Link), plus backfill of CRM Deal.contact
     scalar if Phase 2 didn't set it.
  4. Opportunity.lost_reasons → CRM Deal.lost_reason scalar (first row)
     + lost_notes append (extras), with auto-create of missing
     CRM Lost Reason on demand.

Per the user's directive: lazy-create dependent records on demand
rather than failing — these reshapes work even if the user marked
Item / Lost Reason as Skip during the lock phase.

Each reshape returns a dict {ok, skipped, failed, last_error,
sample_failed} that the runner folds into the CRM Migration Run Step
counters. Per-row exceptions are caught and counted; the orchestrator
never stops on a bad row.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

SAMPLE_FAILED_LIMIT = 50
CHUNK_SIZE = 500


def _empty_result() -> dict:
	return {"ok": 0, "skipped": 0, "failed": 0, "last_error": "", "sample_failed": []}


def _bump_failed(result: dict, key: str, error: Exception) -> None:
	result["failed"] += 1
	result["last_error"] = str(error)[:500]
	if len(result["sample_failed"]) < SAMPLE_FAILED_LIMIT:
		result["sample_failed"].append(key)


# ---------------------------------------------------------------------------
# 1. Prospect contacts → CRM Organization Contact.links
# ---------------------------------------------------------------------------

def reshape_prospect_contacts() -> dict:
	"""For every Contact.links row pointing at Prospect, add an equivalent
	row pointing at CRM Organization with the same link_name (preserved by
	Phase 2). Idempotent — duplicates are skipped.
	"""
	result = _empty_result()

	# `tabContact` doesn't have a separate child table; Dynamic Link rows
	# are themselves the child rows under tabDynamic Link with parent =
	# Contact name.
	prospect_links = frappe.db.sql(
		"""
		SELECT name, parent, link_name
		FROM `tabDynamic Link`
		WHERE parenttype = 'Contact'
		  AND parentfield = 'links'
		  AND link_doctype = 'Prospect'
		""",
		as_dict=True,
	)
	if not prospect_links:
		return result

	# Pre-fetch every CRM Organization-linked row so we can dedupe in O(1).
	existing = frappe.db.sql(
		"""
		SELECT parent, link_name
		FROM `tabDynamic Link`
		WHERE parenttype = 'Contact'
		  AND parentfield = 'links'
		  AND link_doctype = 'CRM Organization'
		""",
		as_dict=True,
	)
	existing_set = {(r["parent"], r["link_name"]) for r in existing}

	# Verify the target CRM Organization exists before linking; if not,
	# skip — the user hasn't migrated that Prospect yet.
	migrated_orgs = set(frappe.db.sql_list("SELECT name FROM `tabCRM Organization`"))

	to_insert: list[tuple] = []
	for row in prospect_links:
		key = f"Contact:{row['parent']} → {row['link_name']}"
		try:
			if (row["parent"], row["link_name"]) in existing_set:
				result["skipped"] += 1
				continue
			if row["link_name"] not in migrated_orgs:
				# Target Organization not migrated yet — skip silently; a
				# later re-run will catch it.
				result["skipped"] += 1
				continue
			to_insert.append(
				(
					frappe.generate_hash(length=10),  # name
					row["parent"],                     # parent (Contact name)
					"Contact",                         # parenttype
					"links",                           # parentfield
					"CRM Organization",                # link_doctype
					row["link_name"],                  # link_name
				)
			)
			existing_set.add((row["parent"], row["link_name"]))
		except Exception as e:
			_bump_failed(result, key, e)

	if to_insert:
		try:
			frappe.db.bulk_insert(
				"Dynamic Link",
				fields=["name", "parent", "parenttype", "parentfield",
					"link_doctype", "link_name"],
				values=to_insert,
				ignore_duplicates=True,
			)
			result["ok"] += len(to_insert)
		except Exception as e:
			result["failed"] += len(to_insert)
			result["last_error"] = f"bulk_insert Dynamic Link: {e}"[:500]
			for v in to_insert[: SAMPLE_FAILED_LIMIT - len(result["sample_failed"])]:
				result["sample_failed"].append(f"Contact:{v[1]} → {v[5]}")

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# 2. Opportunity Items → CRM Products (with auto-create CRM Product)
# ---------------------------------------------------------------------------

# Opportunity Item → CRM Products: only the columns CRM Products supports.
_ITEM_TO_PRODUCT_FIELDS = {
	"item_code": "product_code",
	"item_name": "product_name",
	"qty": "qty",
	"rate": "rate",
	"amount": "amount",
}

# Item → CRM Product (target = lookup record). Only includes fields present
# on both — image, description, standard_rate are no-ops if absent on
# either side.
_ITEM_TO_CRM_PRODUCT_FIELDS = {
	"item_code": "product_code",
	"item_name": "product_name",
	"disabled": "disabled",
	"image": "image",
	"description": "description",
	"standard_rate": "standard_rate",
}

_PRESERVED_META = ("name", "owner", "creation", "modified", "modified_by")


def _ensure_crm_product(item_code: str, cache: set[str]) -> tuple[bool, str]:
	"""Ensure a CRM Product row exists for `item_code`. Returns (created, error).

	Lazy-creates from `tabItem`, preserving source meta. `cache` is the
	caller's running set of names known to exist (avoids per-row DB checks).
	"""
	if item_code in cache:
		return False, ""
	if frappe.db.exists("CRM Product", item_code):
		cache.add(item_code)
		return False, ""

	# Source row from tabItem (if absent, we can't auto-create — caller will
	# log and skip the dependent CRM Products row).
	item_row = frappe.db.sql(
		"SELECT * FROM `tabItem` WHERE name = %s",
		(item_code,),
		as_dict=True,
	)
	if not item_row:
		return False, f"tabItem row '{item_code}' missing — can't auto-create CRM Product"
	item_row = item_row[0]

	target = {f: item_row.get(f) for f in _PRESERVED_META}
	for src, tgt in _ITEM_TO_CRM_PRODUCT_FIELDS.items():
		target[tgt] = item_row.get(src)

	# Set required defaults
	target.setdefault("docstatus", 0)
	# CRM Product autoname is field:product_code → name = item_code, so
	# preserving the source name aligns with the autoname rule.
	target["name"] = item_code

	cols = list(target.keys())
	values = [tuple(target[c] for c in cols)]
	frappe.db.bulk_insert(
		"CRM Product",
		fields=cols,
		values=values,
		ignore_duplicates=True,
	)
	cache.add(item_code)
	return True, ""


def reshape_opportunity_items() -> dict:
	"""For every Opportunity, copy its Opportunity Item rows to CRM Products
	rows under the corresponding CRM Deal. Lazily auto-creates CRM Product
	records as needed.
	"""
	result = _empty_result()

	# Run only against Opportunities that have already been migrated as
	# CRM Deals — child rows whose parent doesn't exist on the target side
	# would orphan.
	migrated_deals = set(frappe.db.sql_list("SELECT name FROM `tabCRM Deal`"))
	if not migrated_deals:
		return result

	# Fetch all Opportunity Items in one shot — typically not huge.
	items = frappe.db.sql(
		"""
		SELECT *
		FROM `tabOpportunity Item`
		WHERE parenttype = 'Opportunity'
		  AND parentfield = 'items'
		""",
		as_dict=True,
	)
	if not items:
		return result

	# Pre-fetch CRM Products names that already exist (idempotency).
	existing_products = set(
		frappe.db.sql_list(
			"SELECT name FROM `tabCRM Products` WHERE parenttype = 'CRM Deal'"
		)
	)

	crm_product_cache: set[str] = set(
		frappe.db.sql_list("SELECT name FROM `tabCRM Product`")
	)

	to_insert: list[tuple] = []
	target_cols = [
		*_PRESERVED_META,
		"idx",
		"parent",
		"parenttype",
		"parentfield",
		"docstatus",
		*_ITEM_TO_PRODUCT_FIELDS.values(),
	]

	for item in items:
		key = f"OpportunityItem:{item.get('name')}"
		try:
			if item["parent"] not in migrated_deals:
				# CRM Deal not migrated yet — skip rather than orphan
				result["skipped"] += 1
				continue

			if item["name"] in existing_products:
				result["skipped"] += 1
				continue

			# Lazy-create CRM Product if missing (Phase 2 may have skipped Item)
			item_code = item.get("item_code")
			if item_code:
				_, err = _ensure_crm_product(item_code, crm_product_cache)
				if err:
					_bump_failed(result, key, RuntimeError(err))
					continue

			row: dict = {f: item.get(f) for f in _PRESERVED_META}
			row["idx"] = item.get("idx") or 1
			row["parent"] = item["parent"]  # CRM Deal name = source Opp name
			row["parenttype"] = "CRM Deal"
			row["parentfield"] = "products"
			row["docstatus"] = item.get("docstatus") or 0
			for src, tgt in _ITEM_TO_PRODUCT_FIELDS.items():
				row[tgt] = item.get(src)

			to_insert.append(tuple(row.get(c) for c in target_cols))
			existing_products.add(item["name"])
		except Exception as e:
			_bump_failed(result, key, e)

	if to_insert:
		try:
			# Insert in chunks so a single bad chunk doesn't poison everything.
			for offset in range(0, len(to_insert), CHUNK_SIZE):
				chunk = to_insert[offset : offset + CHUNK_SIZE]
				frappe.db.bulk_insert(
					"CRM Products",
					fields=target_cols,
					values=chunk,
					ignore_duplicates=True,
				)
			result["ok"] += len(to_insert)
		except Exception as e:
			result["failed"] += len(to_insert)
			result["last_error"] = f"bulk_insert CRM Products: {e}"[:500]

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# 3. Opportunity contact_person + Contact.links → CRM Deal.contacts
# ---------------------------------------------------------------------------

def reshape_opportunity_contacts() -> dict:
	"""Build CRM Deal.contacts child rows from Opportunity.contact_person
	plus any Contact.links pointing at the Opportunity's party_name (the
	standard ERPNext Contact ↔ Prospect Dynamic Link pattern).
	"""
	result = _empty_result()

	# Need both party_name and contact_person from each migrated Opportunity.
	deals = frappe.db.sql(
		"""
		SELECT o.name AS opp_name,
		       o.party_name AS party_name,
		       o.contact_person AS contact_person
		FROM `tabOpportunity` o
		INNER JOIN `tabCRM Deal` cd ON cd.name = o.name
		""",
		as_dict=True,
	)
	if not deals:
		return result

	# Pre-fetch existing CRM Contacts for idempotency: key = (parent, contact)
	existing = set(
		frappe.db.sql(
			"""
			SELECT parent, contact
			FROM `tabCRM Contacts`
			WHERE parenttype = 'CRM Deal'
			"""
		)
	)

	# Pre-fetch all Prospect-linked contacts in one query, group by Prospect.
	prospect_links = frappe.db.sql(
		"""
		SELECT parent AS contact_name, link_name AS prospect_name
		FROM `tabDynamic Link`
		WHERE parenttype = 'Contact'
		  AND parentfield = 'links'
		  AND link_doctype = 'Prospect'
		""",
		as_dict=True,
	)
	contacts_by_prospect: dict[str, list[str]] = {}
	for r in prospect_links:
		contacts_by_prospect.setdefault(r["prospect_name"], []).append(r["contact_name"])

	# Pre-fetch contact details (full_name, gender) and primary email/phone
	# for every contact we'll touch.
	all_contact_names: set[str] = set()
	for names in contacts_by_prospect.values():
		all_contact_names.update(names)
	for d in deals:
		if d["contact_person"]:
			all_contact_names.add(d["contact_person"])

	contact_details: dict[str, dict] = {}
	if all_contact_names:
		rows = frappe.db.sql(
			"""
			SELECT name, full_name, gender, email_id, mobile_no, phone
			FROM `tabContact`
			WHERE name IN %(names)s
			""",
			{"names": tuple(all_contact_names)},
			as_dict=True,
		)
		for r in rows:
			contact_details[r["name"]] = r

	target_cols = [
		"name",
		"idx",
		"parent",
		"parenttype",
		"parentfield",
		"docstatus",
		"contact",
		"full_name",
		"email",
		"gender",
		"mobile_no",
		"phone",
		"is_primary",
	]

	to_insert: list[tuple] = []
	scalar_backfills: list[tuple[str, str]] = []  # (deal_name, primary_contact)

	for d in deals:
		opp_name = d["opp_name"]
		party = d["party_name"]
		primary = d["contact_person"]

		# Source contact list: every Contact whose Contact.links points at
		# this Prospect first, falling back to the single contact_person
		# scalar if that lookup is empty.
		contacts = list(contacts_by_prospect.get(party, []))
		if not contacts and primary:
			contacts = [primary]
		elif primary and primary not in contacts:
			# Make sure primary is included even if Contact.links didn't
			# capture it.
			contacts.append(primary)

		if not contacts:
			continue

		for idx, contact_name in enumerate(contacts, start=1):
			key = f"CRMContacts:{opp_name}/{contact_name}"
			try:
				if (opp_name, contact_name) in existing:
					result["skipped"] += 1
					continue
				details = contact_details.get(contact_name) or {}
				row = (
					frappe.generate_hash(length=10),  # name
					idx,
					opp_name,                          # parent (CRM Deal name = Opp name)
					"CRM Deal",
					"contacts",
					0,                                 # docstatus
					contact_name,
					details.get("full_name") or contact_name,
					details.get("email_id"),
					details.get("gender"),
					details.get("mobile_no"),
					details.get("phone"),
					1 if contact_name == primary else 0,
				)
				to_insert.append(row)
				existing.add((opp_name, contact_name))
			except Exception as e:
				_bump_failed(result, key, e)

		# If no contact was marked primary (i.e. opportunity.contact_person
		# was empty but Contact.links had rows), the first contact in the
		# list becomes primary.
		if primary is None or not primary:
			if contacts:
				scalar_backfills.append((opp_name, contacts[0]))

	if to_insert:
		try:
			for offset in range(0, len(to_insert), CHUNK_SIZE):
				chunk = to_insert[offset : offset + CHUNK_SIZE]
				frappe.db.bulk_insert(
					"CRM Contacts",
					fields=target_cols,
					values=chunk,
					ignore_duplicates=True,
				)
			result["ok"] += len(to_insert)
		except Exception as e:
			result["failed"] += len(to_insert)
			result["last_error"] = f"bulk_insert CRM Contacts: {e}"[:500]

	# Backfill CRM Deal.contact scalar where empty
	for deal_name, primary_contact in scalar_backfills:
		try:
			current = frappe.db.get_value("CRM Deal", deal_name, "contact")
			if not current:
				frappe.db.set_value(
					"CRM Deal", deal_name, "contact", primary_contact,
					update_modified=False,
				)
		except Exception as e:
			_bump_failed(result, f"backfill {deal_name}.contact", e)

	# Mark the matching row primary if no row ended up flagged (when the row
	# was created on a previous run with is_primary=0 because Phase 2 hadn't
	# set contact_person yet).
	for d in deals:
		if not d["contact_person"]:
			continue
		try:
			frappe.db.sql(
				"""
				UPDATE `tabCRM Contacts`
				SET is_primary = CASE WHEN contact = %(primary)s THEN 1 ELSE 0 END
				WHERE parenttype = 'CRM Deal'
				  AND parent = %(parent)s
				""",
				{"primary": d["contact_person"], "parent": d["opp_name"]},
			)
		except Exception as e:
			_bump_failed(result, f"primary-flag {d['opp_name']}", e)

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# 4. Opportunity.lost_reasons → CRM Deal.lost_reason (+ auto-create)
# ---------------------------------------------------------------------------

def _ensure_crm_lost_reason(lost_reason: str, cache: set[str]) -> tuple[bool, str]:
	if lost_reason in cache:
		return False, ""
	if frappe.db.exists("CRM Lost Reason", lost_reason):
		cache.add(lost_reason)
		return False, ""

	src = frappe.db.sql(
		"SELECT * FROM `tabOpportunity Lost Reason` WHERE name = %s",
		(lost_reason,),
		as_dict=True,
	)
	if not src:
		return False, f"tabOpportunity Lost Reason '{lost_reason}' missing"
	src = src[0]

	target = {f: src.get(f) for f in _PRESERVED_META}
	target["name"] = lost_reason
	target["lost_reason"] = src.get("lost_reason") or lost_reason
	target.setdefault("docstatus", 0)

	cols = list(target.keys())
	values = [tuple(target[c] for c in cols)]
	frappe.db.bulk_insert(
		"CRM Lost Reason",
		fields=cols,
		values=values,
		ignore_duplicates=True,
	)
	cache.add(lost_reason)
	return True, ""


def reshape_opportunity_lost_reasons() -> dict:
	"""Take each Opportunity's lost_reasons table; set CRM Deal.lost_reason
	(if empty) to the first row, append extras to lost_notes.
	"""
	result = _empty_result()

	migrated_deals = set(frappe.db.sql_list("SELECT name FROM `tabCRM Deal`"))
	if not migrated_deals:
		return result

	# Source rows: child of Opportunity; child doctype = "Opportunity Lost Reason"
	# (yes, same name as the lookup; the Table MultiSelect uses the same DocType
	# name for its child rows in some setups). Detect the actual child doctype
	# from the source meta to be safe.
	opp_meta = frappe.get_meta("Opportunity")
	lr_field = opp_meta.get_field("lost_reasons")
	if lr_field is None:
		return result
	child_dt = lr_field.options
	if not child_dt:
		return result

	rows = frappe.db.sql(
		f"""
		SELECT parent, lost_reason
		FROM `tab{child_dt}`
		WHERE parenttype = 'Opportunity'
		  AND parentfield = 'lost_reasons'
		ORDER BY parent, idx
		""",
		as_dict=True,
	)
	if not rows:
		return result

	# Group by Opportunity name
	per_opp: dict[str, list[str]] = {}
	for r in rows:
		per_opp.setdefault(r["parent"], []).append(r["lost_reason"])

	cache: set[str] = set(frappe.db.sql_list("SELECT name FROM `tabCRM Lost Reason`"))

	for opp_name, reasons in per_opp.items():
		key = f"LostReason:{opp_name}"
		try:
			if opp_name not in migrated_deals:
				result["skipped"] += 1
				continue

			# Lazy-create any missing CRM Lost Reason
			for r in reasons:
				_, err = _ensure_crm_lost_reason(r, cache)
				if err:
					_bump_failed(result, key, RuntimeError(err))
					continue

			# Set scalar if empty
			changed = False
			current = frappe.db.get_value("CRM Deal", opp_name, "lost_reason")
			if not current:
				frappe.db.set_value(
					"CRM Deal", opp_name, "lost_reason", reasons[0],
					update_modified=False,
				)
				changed = True

			# Append extras to lost_notes if multiple
			if len(reasons) > 1:
				existing_notes = cstr(
					frappe.db.get_value("CRM Deal", opp_name, "lost_notes")
				)
				extras = ", ".join(reasons[1:])
				marker = f"Other reasons: {extras}"
				if marker not in existing_notes:
					new_notes = (
						f"{existing_notes}\n\n{marker}"
						if existing_notes.strip()
						else marker
					)
					frappe.db.set_value(
						"CRM Deal", opp_name, "lost_notes", new_notes,
						update_modified=False,
					)
					changed = True

			if changed:
				result["ok"] += 1
			else:
				result["skipped"] += 1
		except Exception as e:
			_bump_failed(result, key, e)

	frappe.db.commit()
	return result


# ---------------------------------------------------------------------------
# Entry-point: which reshapes apply to a given source step?
# ---------------------------------------------------------------------------

def reshape_for(source_doctype: str) -> dict:
	"""Return totals dict for all reshapes applicable to one source step."""
	totals = _empty_result()

	if source_doctype == "Prospect":
		_merge(totals, reshape_prospect_contacts())
	elif source_doctype == "Opportunity":
		_merge(totals, reshape_opportunity_items())
		_merge(totals, reshape_opportunity_contacts())
		_merge(totals, reshape_opportunity_lost_reasons())

	return totals


def _merge(totals: dict, partial: dict) -> None:
	totals["ok"] += partial["ok"]
	totals["skipped"] += partial["skipped"]
	totals["failed"] += partial["failed"]
	if partial["last_error"]:
		totals["last_error"] = partial["last_error"]
	for s in partial["sample_failed"]:
		if len(totals["sample_failed"]) < SAMPLE_FAILED_LIMIT:
			totals["sample_failed"].append(s)

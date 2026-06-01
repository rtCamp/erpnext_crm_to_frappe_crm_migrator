"""Schema reshape between source and target child tables.

Each `reshape_*` function returns `{ok, skipped, failed, last_error,
sample_failed}` that the runner folds into the CRM Migration Run Step
counters. See `docs/architecture.md` for the reshape pipeline and
`docs/decisions.md` for the bulk_insert / source-meta / repoint rationale.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Count, Lower
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
	# Persist the full traceback to the Error Log so the user has more
	# than the 500-char excerpt on the Run Step row.
	frappe.log_error(
		title=f"Migrator reshape: {key}",
		message=frappe.get_traceback(),
	)


# ---------------------------------------------------------------------------
# 1. Dynamic Link repoint (Contact.links + Address.links)
# ---------------------------------------------------------------------------

# source → target doctype renames for Contact.links / Address.links repoint.
# Source name == target name (the core records runner preserves source
# meta), so only link_doctype needs to change — link_name is left alone.
_DYNAMIC_LINK_REPOINTS: dict[str, str] = {
	"Lead": "CRM Lead",
	"Opportunity": "CRM Deal",
	"Prospect": "CRM Organization",
}

# parenttypes whose `links` Table holds the Dynamic Link rows we care about.
_DYNAMIC_LINK_PARENTS: tuple[str, ...] = ("Contact", "Address")


def reshape_dynamic_links() -> dict:
	"""Re-point `tabDynamic Link` rows on Contact + Address from the
	ERPNext source doctypes (Lead / Opportunity / Prospect) to their
	Frappe CRM equivalents.

	One UPDATE per (parenttype, source) combination — 6 total. `link_name`
	is preserved verbatim because the core records runner keeps source
	`name` on the target row, so the linkage continues to resolve
	against the migrated CRM-side record. Idempotent — once flipped, the WHERE filter matches
	no rows.

	Replaces the earlier "ADD new Contact.links rows pointing at CRM
	Organization" pattern, which doubled the dataset and only covered
	Contact-side Prospect linkages. Repointing covers all three source
	doctypes for both Contact and Address sides in one shot, and leaves
	no orphan Dynamic Link rows after the post-migration cleanup deletes
	the source doctype rows.
	"""
	result = _empty_result()

	for parenttype in _DYNAMIC_LINK_PARENTS:
		for src_dt, tgt_dt in _DYNAMIC_LINK_REPOINTS.items():
			matched = frappe.db.count(
				"Dynamic Link",
				{"parenttype": parenttype, "link_doctype": src_dt},
			)
			if not matched:
				continue

			try:
				dl = frappe.qb.DocType("Dynamic Link")
				(
					frappe.qb.update(dl)
					.set(dl.link_doctype, tgt_dt)
					.where((dl.parenttype == parenttype) & (dl.link_doctype == src_dt))
					.run()
				)
				result["ok"] += int(matched)
			except Exception as e:
				_bump_failed(result, f"{parenttype}.links: {src_dt} → {tgt_dt}", e)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
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
	item_tbl = frappe.qb.DocType("Item")
	item_row = (frappe.qb.from_(item_tbl).select(item_tbl.star).where(item_tbl.name == item_code)).run(
		as_dict=True
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
	deal_tbl = frappe.qb.DocType("CRM Deal")
	migrated_deals = set(r[0] for r in frappe.qb.from_(deal_tbl).select(deal_tbl.name).run())
	if not migrated_deals:
		return result

	# Fetch all Opportunity Items in one shot — typically not huge.
	oi = frappe.qb.DocType("Opportunity Item")
	items = (
		frappe.qb.from_(oi)
		.select(oi.star)
		.where((oi.parenttype == "Opportunity") & (oi.parentfield == "items"))
	).run(as_dict=True)
	if not items:
		return result

	# Pre-fetch CRM Products names that already exist (idempotency).
	crm_products = frappe.qb.DocType("CRM Products")
	existing_products = set(
		r[0]
		for r in frappe.qb.from_(crm_products)
		.select(crm_products.name)
		.where(crm_products.parenttype == "CRM Deal")
		.run()
	)

	crm_product = frappe.qb.DocType("CRM Product")
	crm_product_cache: set[str] = set(
		r[0] for r in frappe.qb.from_(crm_product).select(crm_product.name).run()
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

			# Lazy-create CRM Product if missing — the user may have
			# marked Item as Skip during the lock phase.
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
			frappe.log_error(
				title="Migrator reshape: CRM Products bulk_insert",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# 3. Opportunity contact_person + Contact.links → CRM Deal.contacts
# ---------------------------------------------------------------------------


def reshape_opportunity_contacts() -> dict:
	"""Build CRM Deal.contacts child rows from Opportunity.contact_person
	plus any Contact.links pointing at the Opportunity's party (using
	the standard ERPNext Contact ↔ party Dynamic Link pattern).

	Looks at Contact.links for whichever doctype the Opportunity points
	at — Prospect, Lead, or Customer — so a from=Lead Opportunity finds
	Contacts linked to that Lead, a from=Customer Opportunity finds
	Contacts linked to the Customer, etc. Falls back to the scalar
	`contact_person` if no linked Contacts are found.
	"""
	result = _empty_result()

	# Need both party_name, opportunity_from, and contact_person.
	opp = frappe.qb.DocType("Opportunity")
	cd = frappe.qb.DocType("CRM Deal")
	deals = (
		frappe.qb.from_(opp)
		.inner_join(cd)
		.on(cd.name == opp.name)
		.select(
			opp.name.as_("opp_name"),
			opp.opportunity_from.as_("opportunity_from"),
			opp.party_name.as_("party_name"),
			opp.contact_person.as_("contact_person"),
		)
	).run(as_dict=True)
	if not deals:
		return result

	# Pre-fetch existing CRM Contacts for idempotency: key = (parent, contact)
	crm_contacts = frappe.qb.DocType("CRM Contacts")
	existing = set(
		frappe.qb.from_(crm_contacts)
		.select(crm_contacts.parent, crm_contacts.contact)
		.where(crm_contacts.parenttype == "CRM Deal")
		.run()
	)

	# Pre-fetch every Contact.links row for the doctypes Opportunity might
	# reference, indexed by (link_doctype, link_name) so we can look up a
	# contact list in O(1) per deal regardless of opportunity_from.
	dl = frappe.qb.DocType("Dynamic Link")
	link_rows = (
		frappe.qb.from_(dl)
		.select(
			dl.parent.as_("contact_name"),
			dl.link_doctype,
			dl.link_name,
		)
		.where(
			(dl.parenttype == "Contact")
			& (dl.parentfield == "links")
			& (dl.link_doctype.isin(["Prospect", "Lead", "Customer"]))
		)
	).run(as_dict=True)
	contacts_by_party: dict[tuple[str, str], list[str]] = defaultdict(list)
	for r in link_rows:
		contacts_by_party[(r["link_doctype"], r["link_name"])].append(r["contact_name"])

	# Email-based fallback for from=Lead opportunities whose Lead has
	# no Contact.links attached. Lots of customers create Contacts and
	# Leads with the same email/mobile_no but never wire them up via
	# Dynamic Link. Pre-fetch a {email_lower → contact_name} map from
	# Contact Email so we can match in O(1) per deal.
	contact_by_email: dict[str, str] = {}
	ce = frappe.qb.DocType("Contact Email")
	for r in (
		frappe.qb.from_(ce)
		.select(ce.parent.as_("contact"), Lower(ce.email_id).as_("email"))
		.where(ce.email_id.isnotnull() & (ce.email_id != ""))
	).run(as_dict=True):
		# First-wins; downstream rules don't dedup so multiple Contacts
		# sharing an email all collapse to one. Acceptable for migration.
		contact_by_email.setdefault(r["email"], r["contact"])

	# Lead email lookup — only loaded when there's a from=Lead deal that
	# might need the email fallback. Lazy via single query keyed by names.
	from_lead_party_names = {
		d["party_name"] for d in deals if d["opportunity_from"] == "Lead" and d["party_name"]
	}
	lead_email: dict[str, str] = {}
	if from_lead_party_names:
		lead = frappe.qb.DocType("Lead")
		for r in (
			frappe.qb.from_(lead)
			.select(lead.name, Lower(lead.email_id).as_("email"))
			.where(
				lead.name.isin(list(from_lead_party_names))
				& lead.email_id.isnotnull()
				& (lead.email_id != "")
			)
		).run(as_dict=True):
			lead_email[r["name"]] = r["email"]

	# Pre-fetch contact details for every contact we'll touch — this
	# includes email-fallback contacts that the per-deal loop below
	# might pick up, so they're already in the details map by then.
	all_contact_names: set[str] = set()
	for names in contacts_by_party.values():
		all_contact_names.update(names)
	for d in deals:
		if d["contact_person"]:
			all_contact_names.add(d["contact_person"])
		# from=Lead with no link-based contacts and no contact_person:
		# preview the email-fallback so its details get fetched.
		if d["opportunity_from"] == "Lead" and d["party_name"]:
			has_linked = bool(contacts_by_party.get((d["opportunity_from"], d["party_name"])))
			if not has_linked and not d["contact_person"]:
				email = lead_email.get(d["party_name"])
				matched = contact_by_email.get(email) if email else None
				if matched:
					all_contact_names.add(matched)

	contact_details: dict[str, dict] = {}
	if all_contact_names:
		contact = frappe.qb.DocType("Contact")
		rows = (
			frappe.qb.from_(contact)
			.select(
				contact.name,
				contact.full_name,
				contact.gender,
				contact.email_id,
				contact.mobile_no,
				contact.phone,
			)
			.where(contact.name.isin(list(all_contact_names)))
		).run(as_dict=True)
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
		ofrom = d["opportunity_from"]
		primary = d["contact_person"]

		# Source contact list, in priority order:
		#   1. Contacts with a Dynamic Link to the Opportunity's party
		#      (Prospect/Lead/Customer) — the structured ERPNext model.
		#   2. The scalar contact_person field on the Opportunity.
		#   3. For from=Lead with neither of the above: a Contact whose
		#      email matches the Lead's email_id. Lots of customers
		#      have Contacts and Leads sharing an email but never wire
		#      them up via Contact.links; this fallback catches them.
		# The primary is always included regardless of how discovered.
		contacts: list[str] = []
		if ofrom and party:
			contacts = list(contacts_by_party.get((ofrom, party), []))
		if not contacts and primary:
			contacts = [primary]
		elif primary and primary not in contacts:
			contacts.append(primary)

		if not contacts and ofrom == "Lead":
			email = lead_email.get(party)
			matched = contact_by_email.get(email) if email else None
			if matched:
				contacts = [matched]

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
					opp_name,  # parent (CRM Deal name = Opp name)
					"CRM Deal",
					"contacts",
					0,  # docstatus
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
			frappe.log_error(
				title="Migrator reshape: CRM Contacts bulk_insert",
				message=frappe.get_traceback(),
			)

	# Backfill CRM Deal.contact scalar where empty. Single CASE-WHEN
	# UPDATE per chunk — the previous per-row set_value loop deadlocked
	# against the preceding tabCRM Contacts bulk_insert's index locks.
	if scalar_backfills:
		cd_upd = frappe.qb.DocType("CRM Deal")
		for offset in range(0, len(scalar_backfills), CHUNK_SIZE):
			chunk = scalar_backfills[offset : offset + CHUNK_SIZE]
			try:
				contact_case = Case()
				for deal_name, primary_contact in chunk:
					contact_case = contact_case.when(cd_upd.name == deal_name, primary_contact)
				chunk_names = [deal_name for deal_name, _ in chunk]
				(
					frappe.qb.update(cd_upd)
					.set(cd_upd.contact, contact_case)
					.where(cd_upd.name.isin(chunk_names) & (cd_upd.contact.isnull() | (cd_upd.contact == "")))
					.run()
				)
			except Exception as e:
				_bump_failed(result, f"backfill CRM Deal.contact (chunk @ {offset})", e)

	# Mark the matching row primary if no row ended up flagged (when the row
	# was created on a previous run with is_primary=0 because the core
	# records runner hadn't set contact_person yet).
	cc_upd = frappe.qb.DocType("CRM Contacts")
	for d in deals:
		if not d["contact_person"]:
			continue
		try:
			(
				frappe.qb.update(cc_upd)
				.set(
					cc_upd.is_primary,
					Case().when(cc_upd.contact == d["contact_person"], 1).else_(0),
				)
				.where((cc_upd.parenttype == "CRM Deal") & (cc_upd.parent == d["opp_name"]))
				.run()
			)
		except Exception as e:
			_bump_failed(result, f"primary-flag {d['opp_name']}", e)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# 4. Opportunity.lost_reasons → CRM Deal.lost_reason (+ auto-create)
# ---------------------------------------------------------------------------


def _ensure_crm_lost_reason(lost_reason: str, cache: set[str]) -> tuple[bool, str]:
	if not lost_reason:
		# Blank Table-MultiSelect row — nothing to ensure. Caller already
		# filters these out, but be defensive: don't surface as a failure.
		return False, ""
	if lost_reason in cache:
		return False, ""
	if frappe.db.exists("CRM Lost Reason", lost_reason):
		cache.add(lost_reason)
		return False, ""

	olr = frappe.qb.DocType("Opportunity Lost Reason")
	src = (frappe.qb.from_(olr).select(olr.star).where(olr.name == lost_reason)).run(as_dict=True)
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
	"""Migrate Opportunity's lost-reason data onto the migrated CRM Deal:

	- First lost_reasons child row → `CRM Deal.lost_reason` (if empty).
	- Extra lost_reasons rows + free-form `Opportunity.order_lost_reason`
	  text → appended to `CRM Deal.lost_notes` with a separator.
	"""
	result = _empty_result()

	deal_tbl = frappe.qb.DocType("CRM Deal")
	migrated_deals = set(r[0] for r in frappe.qb.from_(deal_tbl).select(deal_tbl.name).run())
	if not migrated_deals:
		return result

	# Source child rows: lost_reasons Table MultiSelect. Detect the child
	# doctype from source meta (varies a little across ERPNext versions).
	opp_meta = frappe.get_meta("Opportunity")
	lr_field = opp_meta.get_field("lost_reasons")
	if lr_field is None or not lr_field.options:
		return result
	child_dt = lr_field.options

	child_tbl = frappe.qb.DocType(child_dt)
	rows = (
		frappe.qb.from_(child_tbl)
		.select(child_tbl.parent, child_tbl.lost_reason)
		.where((child_tbl.parenttype == "Opportunity") & (child_tbl.parentfield == "lost_reasons"))
		.orderby(child_tbl.parent)
		.orderby(child_tbl.idx)
	).run(as_dict=True)
	# Group lost_reasons by Opportunity, dropping empty rows.
	per_opp: dict[str, list[str]] = {}
	for r in rows:
		lr = (r["lost_reason"] or "").strip()
		if not lr:
			continue
		per_opp.setdefault(r["parent"], []).append(lr)

	# Free-form detailed-reason text on the Opportunity parent. Only pull
	# if the column exists (it's vanilla ERPNext, but defensively check).
	detail_text: dict[str, str] = {}
	if "order_lost_reason" in frappe.db.get_table_columns("Opportunity"):
		opp = frappe.qb.DocType("Opportunity")
		text_rows = (
			frappe.qb.from_(opp)
			.select(opp.name, opp.order_lost_reason)
			.where(opp.order_lost_reason.isnotnull() & (opp.order_lost_reason != ""))
		).run(as_dict=True)
		detail_text = {r["name"]: r["order_lost_reason"].strip() for r in text_rows}

	if not per_opp and not detail_text:
		return result

	clr = frappe.qb.DocType("CRM Lost Reason")
	cache: set[str] = set(r[0] for r in frappe.qb.from_(clr).select(clr.name).run())
	all_opps = set(per_opp) | set(detail_text)

	for opp_name in all_opps:
		key = f"LostReason:{opp_name}"
		try:
			if opp_name not in migrated_deals:
				result["skipped"] += 1
				continue

			reasons = per_opp.get(opp_name, [])
			detail = detail_text.get(opp_name, "")

			# Lazy-create CRM Lost Reasons for each child entry.
			for r in reasons:
				_, err = _ensure_crm_lost_reason(r, cache)
				if err:
					_bump_failed(result, key, RuntimeError(err))
					continue

			changed = False

			# Set scalar lost_reason if a child row exists and target empty.
			if reasons:
				current = frappe.db.get_value("CRM Deal", opp_name, "lost_reason")
				if not current:
					frappe.db.set_value(
						"CRM Deal",
						opp_name,
						"lost_reason",
						reasons[0],
						update_modified=False,
					)
					changed = True

			# Build the lost_notes append block: extras + free-form detail.
			parts: list[str] = []
			if len(reasons) > 1:
				parts.append(f"Other reasons: {', '.join(reasons[1:])}")
			if detail:
				parts.append(f"Detailed: {detail}")

			if parts:
				marker = "\n\n".join(parts)
				existing_notes = cstr(frappe.db.get_value("CRM Deal", opp_name, "lost_notes"))
				if marker not in existing_notes:
					new_notes = f"{existing_notes}\n\n{marker}" if existing_notes.strip() else marker
					frappe.db.set_value(
						"CRM Deal",
						opp_name,
						"lost_notes",
						new_notes,
						update_modified=False,
					)
					changed = True

			if changed:
				result["ok"] += 1
			else:
				result["skipped"] += 1
		except Exception as e:
			_bump_failed(result, key, e)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# 5. Native ERPNext notes (tabCRM Note child rows) → standalone FCRM Note docs
# ---------------------------------------------------------------------------

# ERPNext Lead/Opportunity/Prospect have a native `notes` Table field
# pointing at the `CRM Note` child doctype. Frappe CRM uses a different
# storage model: standalone `FCRM Note` documents with reference_doctype
# + reference_docname pointing back at CRM Lead/CRM Deal/CRM Organization.
# This reshape materialises one FCRM Note per source CRM Note row.
_SOURCE_TO_CRM_TARGET = {
	"Lead": "CRM Lead",
	"Opportunity": "CRM Deal",
	"Prospect": "CRM Organization",
}


def _derive_note_title(custom_title: str | None, note_html: str | None) -> str:
	"""Build a usable title for FCRM Note from the source row.

	Prefer `custom_title` if set, else strip HTML from the note body and
	take the first ~80 chars; fall back to "Note" so the field is never
	empty (FCRM Note's title is a Data field — empty would render as
	'Untitled' on the form).
	"""
	if custom_title:
		return custom_title.strip()[:140] or "Note"
	if not note_html:
		return "Note"
	import re

	text = re.sub(r"<[^>]+>", " ", note_html)
	text = re.sub(r"\s+", " ", text).strip()
	return text[:80] or "Note"


# Hidden marker installed on FCRM Note at runtime so the reset script can
# distinguish migrator-created rows from user-created ones. Mirrors the
# `custom_source_todo` pattern on CRM Task.
_NOTE_MARKER_FIELD = "custom_source_crm_note"


def _ensure_note_marker_field() -> None:
	"""Install the hidden marker field on FCRM Note if not already present."""
	if not frappe.db.exists("DocType", "FCRM Note"):
		return
	if frappe.db.exists("Custom Field", {"dt": "FCRM Note", "fieldname": _NOTE_MARKER_FIELD}):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"FCRM Note": [
				{
					"fieldname": _NOTE_MARKER_FIELD,
					"label": "Source ERPNext CRM Note",
					"fieldtype": "Data",
					"hidden": 1,
					"no_copy": 1,
					"read_only": 1,
					"description": (
						"ERPNext CRM Note.name this FCRM Note was migrated from. "
						"Used by the migrator for idempotency / reset; safe to ignore."
					),
				}
			]
		},
		update=True,
	)


def _resolve_source_note_title_field() -> str | None:
	"""Pick the source CRM Note column to use as the human title.

	Priority: `custom_title`, then `title`. Returns None if neither is
	present — caller falls back to stripping the note body.
	"""
	meta = frappe.get_meta("CRM Note")
	for candidate in ("custom_title", "title"):
		if meta.has_field(candidate):
			return candidate
	return None


# Source/target fields already handled by reshape_notes — exclude from
# the scalar carry-through to avoid double-writing.
_NOTE_RESERVED_FIELDS = {
	"name",
	"parent",
	"parenttype",
	"parentfield",
	"idx",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
	"title",
	"content",
	"reference_doctype",
	"reference_docname",
	"note",
	"added_by",
	"added_on",
	"custom_title",  # source-side title shape, handled by title resolver
}


def _shared_scalar_note_fields(title_field: str | None) -> list[str]:
	"""Same-name, non-Table fields present on BOTH `CRM Note` and `FCRM Note`.

	Used to carry custom-field values (e.g. `custom_parent_note`) verbatim
	from source rows into the migrated FCRM Note. Tables are deliberately
	skipped — those are re-anchored by `_reanchor_note_children`.
	"""
	src_meta = frappe.get_meta("CRM Note")
	tgt_meta = frappe.get_meta("FCRM Note")
	tgt_names = {df.fieldname for df in tgt_meta.fields}
	out: list[str] = []
	for df in src_meta.fields:
		if df.fieldname in _NOTE_RESERVED_FIELDS or df.fieldname == title_field:
			continue
		if df.fieldtype in ("Table", "Table MultiSelect"):
			continue
		if df.fieldtype in ("Tab Break", "Column Break", "Section Break", "HTML"):
			continue
		if df.fieldname in tgt_names:
			out.append(df.fieldname)
	return out


def _reanchor_note_children() -> int:
	"""Re-anchor child rows of any Table field on source CRM Note that has
	a same-named, same-options Table field on FCRM Note.

	JOINs `tabFCRM Note` directly on `f.name = c.parent` — the migrator
	preserves the source CRM Note's name as the FCRM Note's name, so the
	parent reference resolves without translation. Idempotent — once
	flipped, rows no longer match `parenttype = 'CRM Note'`.

	Skips silently if the matching Table field doesn't exist on FCRM Note
	yet (e.g. the customer hasn't installed the mirrored custom field on
	the target). The user can add it and re-run.
	"""
	src_meta = frappe.get_meta("CRM Note")
	tgt_meta = frappe.get_meta("FCRM Note")
	tgt_table_fields = {df.fieldname: df.options for df in tgt_meta.fields if df.fieldtype == "Table"}

	total = 0
	for df in src_meta.fields:
		if df.fieldtype != "Table":
			continue
		if tgt_table_fields.get(df.fieldname) != df.options:
			continue

		child_dt = df.options
		# Pre-count via a JOIN-based qb SELECT — converted from raw SQL.
		c = frappe.qb.DocType(child_dt)
		f = frappe.qb.DocType("FCRM Note")
		count_row = (
			frappe.qb.from_(c)
			.inner_join(f)
			.on(f.name == c.parent)
			.select(Count("*"))
			.where((c.parenttype == "CRM Note") & (c.parentfield == df.fieldname))
		).run()
		n = int(count_row[0][0]) if count_row else 0
		if not n:
			continue

		# UPDATE...JOIN can't be expressed cleanly in pypika; the JOIN guards
		# against re-anchoring children whose CRM Note has not been migrated
		# to FCRM Note yet, so we keep the raw SQL here.
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- child_dt is df.options from source meta (a DocType name), not user input; table names can't be parameterised in SQL
			f"""
			UPDATE `tab{child_dt}` c
			JOIN `tabFCRM Note` f ON f.name = c.parent
			SET c.parenttype = 'FCRM Note'
			WHERE c.parenttype = 'CRM Note'
			  AND c.parentfield = %s
			""",
			(df.fieldname,),
		)
		total += n

	return total


def reshape_notes(source_doctype: str) -> dict:
	"""Convert ERPNext native CRM Note children under one source doctype
	into standalone FCRM Note documents anchored to the migrated CRM
	target row.

	Naming: the source `CRM Note` doctype uses `autoname=autoincrement`
	(integer names). FCRM Note's `name` column is varchar(140) which
	stores the stringified integer fine — the source name is preserved
	verbatim so audit references (Comment, Version, etc.) that point at
	the CRM Note by name continue to resolve after the activity rewrite
	flips their `reference_doctype` from "CRM Note" to "FCRM Note".
	The hidden `custom_source_crm_note` marker (installed at runtime)
	tags migrator-created rows for the reset script.

	Title resolution: prefers `custom_title` (rtcamp/frappe_crm_xt
	convention) or `title` on source CRM Note when present; otherwise
	derives a title by stripping HTML from the note body.

	Child Table re-anchor: any Table field on source CRM Note that also
	exists with the same options on FCRM Note (e.g. `custom_note_attachments`
	→ `NCRM Attachments`) has its children re-anchored to the migrated
	FCRM Note row.

	Source-meta preservation: owner defaults to `added_by`, creation
	defaults to `added_on`. The activity rewrite step is irrelevant for
	these notes — their reference_doctype is already set to a CRM
	target on creation.

	Skipped silently if the migrated parent doesn't exist on the target
	side yet (e.g. user hasn't run the parent step), so a partial
	migration doesn't orphan-reference.
	"""
	target_doctype = _SOURCE_TO_CRM_TARGET.get(source_doctype)
	result = _empty_result()
	if not target_doctype:
		return result
	if not frappe.db.exists("DocType", "CRM Note"):
		return result
	if not frappe.db.exists("DocType", "FCRM Note"):
		return result

	_ensure_note_marker_field()

	title_field = _resolve_source_note_title_field()

	# Scalar carry-through: any non-Table field that exists with the same
	# name on both CRM Note (source) and FCRM Note (target) — covers
	# customisations like `custom_parent_note` that frappe_crm_xt mirrors
	# on FCRM Note. The Table-typed customisations (e.g.
	# `custom_note_attachments`) are handled separately by
	# `_reanchor_note_children` after the bulk insert.
	carry_fields = _shared_scalar_note_fields(title_field)

	crm_note = frappe.qb.DocType("CRM Note")
	select_fields = [
		crm_note.name,
		crm_note.parent,
		crm_note.owner,
		crm_note.creation,
		crm_note.modified,
		crm_note.modified_by,
		crm_note.docstatus,
		crm_note.note,
		crm_note.added_by,
		crm_note.added_on,
	]
	if title_field:
		select_fields.append(crm_note[title_field].as_("note_title"))
	for cf in carry_fields:
		select_fields.append(crm_note[cf])

	rows = (
		frappe.qb.from_(crm_note)
		.select(*select_fields)
		.where((crm_note.parenttype == source_doctype) & (crm_note.parentfield == "notes"))
	).run(as_dict=True)
	if not rows:
		return result

	tgt_tbl = frappe.qb.DocType(target_doctype)
	migrated_targets = set(r[0] for r in frappe.qb.from_(tgt_tbl).select(tgt_tbl.name).run())
	# Identify already-migrated FCRM Notes by the marker field (mirror of
	# the `custom_source_todo` pattern on CRM Task).
	fcrm_note = frappe.qb.DocType("FCRM Note")
	existing_markers = set(
		r[0]
		for r in frappe.qb.from_(fcrm_note)
		.select(fcrm_note[_NOTE_MARKER_FIELD])
		.where(fcrm_note[_NOTE_MARKER_FIELD].isnotnull())
		.run()
	)

	target_cols = [
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"title",
		"content",
		"reference_doctype",
		"reference_docname",
		_NOTE_MARKER_FIELD,
		*carry_fields,
	]
	to_insert: list[tuple] = []

	for r in rows:
		marker = str(r["name"])
		target_name = marker  # preserve source name verbatim
		key = f"FCRMNote:{target_name}"
		try:
			if marker in existing_markers:
				result["skipped"] += 1
				continue
			if r["parent"] not in migrated_targets:
				# Parent CRM target row not migrated yet — skip; a
				# later re-run will catch this once the parent step runs.
				result["skipped"] += 1
				continue

			owner = r.get("added_by") or r.get("owner") or "Administrator"
			creation = r.get("added_on") or r.get("creation")
			modified = r.get("modified") or creation
			modified_by = owner

			to_insert.append(
				(
					target_name,
					owner,
					creation,
					modified,
					modified_by,
					r.get("docstatus") or 0,
					_derive_note_title(r.get("note_title"), r.get("note")),
					r.get("note") or "",
					target_doctype,
					r["parent"],
					marker,
					*(r.get(cf) for cf in carry_fields),
				)
			)
			existing_markers.add(marker)
		except Exception as e:
			_bump_failed(result, key, e)

	if to_insert:
		try:
			for offset in range(0, len(to_insert), CHUNK_SIZE):
				chunk = to_insert[offset : offset + CHUNK_SIZE]
				frappe.db.bulk_insert(
					"FCRM Note",
					fields=target_cols,
					values=chunk,
					ignore_duplicates=True,
				)
			result["ok"] += len(to_insert)
		except Exception as e:
			result["failed"] += len(to_insert)
			result["last_error"] = f"bulk_insert FCRM Note: {e}"[:500]
			frappe.log_error(
				title="Migrator reshape: FCRM Note bulk_insert",
				message=frappe.get_traceback(),
			)

	# Re-anchor any Table-field children (e.g. `custom_note_attachments`)
	# whose source CRM Note now has a corresponding FCRM Note row.
	try:
		result["ok"] += _reanchor_note_children()
	except Exception as e:
		result["failed"] += 1
		result["last_error"] = (
			f"reanchor note children: {e}"
			if not result["last_error"]
			else f"{result['last_error']} | reanchor: {e}"
		)[:500]
		frappe.log_error(
			title="Migrator reshape: note children re-anchor",
			message=frappe.get_traceback(),
		)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# 6. Synthesise ToDo rows from the migrated _assign JSON cache
# ---------------------------------------------------------------------------


def reshape_assignments(source_doctype: str) -> dict:
	"""Build ToDo rows on the CRM target side from the `_assign` JSON
	cache that the core records runner carried over from the source row.

	Why: the CRM frontend's detail page reads `tabToDo` to render the
	assignment widget — `_assign` alone is enough for the list view but
	not for the detail page. On many ERPNext sites the source `_assign`
	cache was populated without matching ToDo rows (e.g. via direct DB
	writes), or any source ToDos may have been deleted before
	migration. The activity rewrite step only *rewrites* existing ToDos;
	this reshape *creates* them from the cache so the detail-page widget
	has data.

	Idempotent: skips when an Open ToDo already exists for the same
	(reference_type, reference_name, allocated_to) — covers the overlap
	with activity-rewritten ToDos.
	"""
	import json

	target_doctype = _SOURCE_TO_CRM_TARGET.get(source_doctype)
	result = _empty_result()
	if not target_doctype:
		return result
	if not frappe.db.exists("DocType", "ToDo"):
		return result

	tgt_tbl = frappe.qb.DocType(target_doctype)
	rows = (
		frappe.qb.from_(tgt_tbl)
		.select(tgt_tbl.name, tgt_tbl.owner, tgt_tbl._assign)
		.where(tgt_tbl._assign.isnotnull() & (tgt_tbl._assign != "") & (tgt_tbl._assign != "[]"))
	).run(as_dict=True)
	if not rows:
		return result

	# Pre-fetch existing Open ToDos for this (reference_name, allocated_to)
	# pair across BOTH the source and target reference_type. The activity
	# rewrite later flips source assignment ToDos from <source> to <target>,
	# so a source-side row blocks duplication just as well as a target-side
	# one — including both here is what makes the dedup correct regardless
	# of step ordering. Source name == target name (preserved by the
	# core records runner), so the same `reference_name` keys both sides.
	todo = frappe.qb.DocType("ToDo")
	existing = set(
		frappe.qb.from_(todo)
		.select(todo.reference_name, todo.allocated_to)
		.where(todo.reference_type.isin([source_doctype, target_doctype]) & (todo.status == "Open"))
		.run()
	)

	target_cols = [
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"status",
		"priority",
		"date",
		"allocated_to",
		"description",
		"reference_type",
		"reference_name",
		"assigned_by",
	]
	to_insert: list[tuple] = []
	now = frappe.utils.now()
	today = frappe.utils.today()
	default_description = f"Assignment migrated from ERPNext {source_doctype}"

	for r in rows:
		try:
			assignees = json.loads(r["_assign"])
		except (json.JSONDecodeError, TypeError):
			continue
		if not isinstance(assignees, list):
			continue

		assigned_by = r.get("owner") or "Administrator"

		for user in assignees:
			if not user:
				continue
			key = (r["name"], user)
			if key in existing:
				result["skipped"] += 1
				continue
			existing.add(key)
			to_insert.append(
				(
					frappe.generate_hash(length=10),
					assigned_by,  # owner of the ToDo row
					now,  # creation
					now,  # modified
					assigned_by,  # modified_by
					0,  # docstatus
					"Open",  # status
					"Medium",  # priority
					today,  # date
					user,  # allocated_to
					default_description,
					target_doctype,  # reference_type
					r["name"],  # reference_name
					assigned_by,  # assigned_by
				)
			)

	if to_insert:
		try:
			for offset in range(0, len(to_insert), CHUNK_SIZE):
				chunk = to_insert[offset : offset + CHUNK_SIZE]
				frappe.db.bulk_insert(
					"ToDo",
					fields=target_cols,
					values=chunk,
					ignore_duplicates=True,
				)
			result["ok"] += len(to_insert)
		except Exception as e:
			result["failed"] += len(to_insert)
			result["last_error"] = f"bulk_insert ToDo: {e}"[:500]
			frappe.log_error(
				title="Migrator reshape: assignments ToDo bulk_insert",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# 7. Convert source ToDos into CRM Tasks (non-assignment ToDos only)
# ---------------------------------------------------------------------------

# Custom field on CRM Task to track which source ToDo each migrated task
# came from. Lazily installed by reshape_tasks on first run.
_TASK_MARKER_FIELD = "custom_source_todo"

# Frappe's assignment ToDos use this description format (see
# frappe/desk/form/assign_to.py:78 — `_("Assignment for {0} {1}")`).
# We filter them out so basic assignment-only ToDos don't become tasks
# the user would have to clean up.
_ASSIGNMENT_DESC_RE = __import__("re").compile(r"^\s*Assignment for \S", flags=__import__("re").IGNORECASE)

# ERPNext ToDo statuses that don't share a name with CRM Task. Everything
# else (Backlog, In Progress, …) is a passthrough — only these three are
# rewritten on the way to CRM Task.
_TODO_STATUS_OVERRIDES = {
	"Open": "Todo",
	"Closed": "Done",
	"Cancelled": "Canceled",
}


def _map_todo_status(source_status: str | None) -> str:
	if not source_status:
		return "Todo"
	return _TODO_STATUS_OVERRIDES.get(source_status, source_status)


def _ensure_task_marker_field() -> None:
	"""Install the hidden tracking field on CRM Task if not already present."""
	if frappe.db.exists("Custom Field", {"dt": "CRM Task", "fieldname": _TASK_MARKER_FIELD}):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"CRM Task": [
				{
					"fieldname": _TASK_MARKER_FIELD,
					"label": "Source ERPNext ToDo",
					"fieldtype": "Data",
					"hidden": 1,
					"no_copy": 1,
					"read_only": 1,
					"description": (
						"ERPNext ToDo.name this CRM Task was migrated from. "
						"Used by the migrator for idempotency / reset; safe to ignore."
					),
				}
			]
		},
		update=True,
	)


def _derive_task_title(custom_title: str | None, description_html: str | None) -> str:
	"""Pick a usable title for the migrated CRM Task.

	Prefer the source ToDo's `custom_title` (a rtcamp/next_crm Custom
	Field that explicitly stores the human-set title), else strip HTML
	from `description` and take the first ~80 chars. Fall back to
	"Task" if both are empty — CRM Task.title is required.
	"""
	if custom_title:
		return custom_title.strip()[:140] or "Task"
	if not description_html:
		return "Task"
	import re

	text = re.sub(r"<[^>]+>", " ", description_html)
	text = re.sub(r"\s+", " ", text).strip()
	return text[:80] or "Task"


def reshape_tasks(source_doctype: str) -> dict:
	"""Convert ERPNext ToDos that reference one source doctype into
	CRM Task documents on the migrated CRM target.

	Skips:
	  - Frappe's auto-assignment ToDos (description matches the
	    "Assignment for <DocType> <name>" template) — those are handled
	    by reshape_assignments which synthesises a tabToDo from
	    `_assign`, not a CRM Task. Treating them as tasks would
	    duplicate every assignment as a task.
	  - ToDos that were already converted in a prior run (custom_source_todo
	    field on CRM Task matches the ToDo.name).
	  - ToDos whose parent CRM target hasn't been migrated yet.

	Uses `frappe.db.bulk_insert` for the CRM Tasks themselves, then
	synthesises the assignment ToDo + `_assign` cache that CRM Task's
	`after_insert` would normally produce. This is faster than per-row
	insert() and sidesteps the assign_to → has_permission → get_meta
	chain that can blow up when an assignee has a stale User Permission
	referencing an uninstalled doctype.
	"""
	import json

	target_doctype = _SOURCE_TO_CRM_TARGET.get(source_doctype)
	result = _empty_result()
	if not target_doctype:
		return result
	if not frappe.db.exists("DocType", "ToDo") or not frappe.db.exists("DocType", "CRM Task"):
		return result

	_ensure_task_marker_field()

	# Source ToDos on this doctype. `custom_title` is a rtcamp/next_crm
	# Custom Field — only pull it if the column actually exists, so the
	# query stays compatible with vanilla Frappe sites.
	todo_columns = (
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"status",
		"priority",
		"date",
		"allocated_to",
		"description",
		"reference_type",
		"reference_name",
		"assigned_by",
	)
	has_custom_title = "custom_title" in frappe.db.get_table_columns("ToDo")
	todo_tbl = frappe.qb.DocType("ToDo")
	todo_select = [todo_tbl[c] for c in todo_columns]
	if has_custom_title:
		todo_select.append(todo_tbl["custom_title"])
	todos = (
		frappe.qb.from_(todo_tbl).select(*todo_select).where(todo_tbl.reference_type == source_doctype)
	).run(as_dict=True)
	if not todos:
		return result

	tgt_tbl = frappe.qb.DocType(target_doctype)
	migrated_targets = set(r[0] for r in frappe.qb.from_(tgt_tbl).select(tgt_tbl.name).run())
	# Already-converted ToDo names (idempotency).
	crm_task = frappe.qb.DocType("CRM Task")
	already_converted = set(
		r[0]
		for r in frappe.qb.from_(crm_task)
		.select(crm_task[_TASK_MARKER_FIELD])
		.where(crm_task[_TASK_MARKER_FIELD].isnotnull())
		.run()
	)

	# CRM Task uses autoname=autoincrement, which Frappe backs with a
	# MariaDB sequence (not a column AUTO_INCREMENT). bulk_insert won't
	# tap the sequence — so without an explicit `name`, every row would
	# land with `name = 0` and ignore_duplicates would drop all but the
	# first. Pre-allocate a name per row via NEXTVAL.
	from frappe.database.sequence import get_next_val

	task_cols = [
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"title",
		"description",
		"assigned_to",
		"status",
		"priority",
		"due_date",
		"reference_doctype",
		"reference_docname",
		_TASK_MARKER_FIELD,
		"_assign",
	]
	task_values: list[tuple] = []
	# Markers we're about to insert — used later to map source ToDo →
	# new CRM Task name when synthesising assignment ToDos.
	new_markers: list[str] = []

	for t in todos:
		key = f"ToDo:{t['name']}"
		try:
			if str(t["name"]) in already_converted:
				result["skipped"] += 1
				continue
			# Skip auto-assignment ToDos — they're not real tasks.
			if _ASSIGNMENT_DESC_RE.match(t.get("description") or ""):
				result["skipped"] += 1
				continue
			if t["reference_name"] not in migrated_targets:
				# Parent CRM target not migrated yet; skip silently.
				result["skipped"] += 1
				continue

			# `assigned_to` on CRM Task is a Data field — it's safe to set
			# directly without going through assign_to(). The matching ToDo
			# row + _assign cache are synthesised below.
			assigned_to = t.get("allocated_to") or None
			_assign_json = json.dumps([assigned_to]) if assigned_to else None

			owner = t.get("owner") or "Administrator"
			creation = t.get("creation")
			modified = t.get("modified") or creation
			modified_by = t.get("modified_by") or owner
			marker = str(t["name"])

			task_values.append(
				(
					get_next_val("CRM Task"),
					owner,
					creation,
					modified,
					modified_by,
					t.get("docstatus") or 0,
					_derive_task_title(t.get("custom_title"), t.get("description")),
					t.get("description") or "",
					assigned_to,
					_map_todo_status(t.get("status")),
					(t.get("priority") or "Medium").capitalize(),
					t.get("date") or None,
					target_doctype,
					t["reference_name"],
					marker,
					_assign_json,
				)
			)
			new_markers.append(marker)
		except Exception as e:
			_bump_failed(result, key, e)

	if task_values:
		try:
			for offset in range(0, len(task_values), CHUNK_SIZE):
				chunk = task_values[offset : offset + CHUNK_SIZE]
				frappe.db.bulk_insert(
					"CRM Task",
					fields=task_cols,
					values=chunk,
					ignore_duplicates=True,
				)
			result["ok"] += len(task_values)
		except Exception as e:
			result["failed"] += len(task_values)
			result["last_error"] = f"bulk_insert CRM Task: {e}"[:500]
			frappe.log_error(
				title="Migrator reshape: CRM Task bulk_insert",
				message=frappe.get_traceback(),
			)
			return result

	# Synthesise the assignment ToDo rows that CRM Task.after_insert would
	# normally produce. Look up the autoincrement names assigned to the
	# tasks we just inserted, then bulk_insert one ToDo per assignee.
	if new_markers:
		ct = frappe.qb.DocType("CRM Task")
		assigned_tasks = (
			frappe.qb.from_(ct)
			.select(
				ct.name,
				ct.owner,
				ct.creation,
				ct.assigned_to,
				ct.due_date,
				ct[_TASK_MARKER_FIELD].as_("marker"),
			)
			.where(
				ct[_TASK_MARKER_FIELD].isin(list(new_markers))
				& ct.assigned_to.isnotnull()
				& (ct.assigned_to != "")
			)
		).run(as_dict=True)

		# Dedup against any pre-existing ToDos so re-runs don't duplicate.
		todo = frappe.qb.DocType("ToDo")
		dedup_names = [str(r["name"]) for r in assigned_tasks] if assigned_tasks else [""]
		existing_assign_keys = set(
			frappe.qb.from_(todo)
			.select(todo.reference_name, todo.allocated_to)
			.where(
				(todo.reference_type == "CRM Task")
				& (todo.status == "Open")
				& (todo.reference_name.isin(dedup_names))
			)
			.run()
		)

		todo_cols = [
			"name",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"docstatus",
			"status",
			"priority",
			"date",
			"allocated_to",
			"description",
			"reference_type",
			"reference_name",
			"assigned_by",
		]
		todo_values: list[tuple] = []
		for row in assigned_tasks:
			task_name = str(row["name"])
			user = row["assigned_to"]
			if (task_name, user) in existing_assign_keys:
				continue
			assigned_by = row["owner"] or "Administrator"
			created = row["creation"] or frappe.utils.now()
			todo_values.append(
				(
					frappe.generate_hash(length=10),
					assigned_by,
					created,
					created,
					assigned_by,
					0,
					"Open",
					"Medium",
					row.get("due_date"),
					user,
					f"Assignment for CRM Task {task_name}",
					"CRM Task",
					task_name,
					assigned_by,
				)
			)

		if todo_values:
			try:
				for offset in range(0, len(todo_values), CHUNK_SIZE):
					chunk = todo_values[offset : offset + CHUNK_SIZE]
					frappe.db.bulk_insert(
						"ToDo",
						fields=todo_cols,
						values=chunk,
						ignore_duplicates=True,
					)
			except Exception as e:
				# Tasks already landed; ToDo failure shouldn't undo them.
				# Log + flag so the user can re-run the assignment-todo
				# step (the next reshape_tasks call will pick up the
				# missing ToDos via the dedup check).
				result["failed"] += len(todo_values)
				result["last_error"] = (
					(result["last_error"] + " | " if result["last_error"] else "")
					+ f"bulk_insert assignment ToDo: {e}"
				)[:500]
				frappe.log_error(
					title="Migrator reshape: CRM Task assignment ToDo bulk_insert",
					message=frappe.get_traceback(),
				)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- function-end barrier — persist this reshape's writes before the next reshape (or activity rewrite) reads them back
	return result


# ---------------------------------------------------------------------------
# Entry-point: which reshapes apply to a given source step?
# ---------------------------------------------------------------------------
#
# Note: Opportunity.custom_stage_change_log → CRM Deal.custom_stage_change_log
# is NOT handled here. Once frappe_crm_xt mirrors the same Table field on
# CRM Deal (pointing at the same CRM Stage Change Log child doctype), the
# runner's `_reanchor_shared_children` re-anchors the rows automatically via
# its same-name + same-options match. The check tolerates sites where the
# doctype or the target custom field is absent — it just skips silently.
# Same story for Opportunity.status_change_log → CRM Deal.status_change_log
# (both Table → CRM Status Change Log).


def reshape_for(source_doctype: str) -> dict:
	"""Return totals dict for all reshapes applicable to one source step."""
	totals = _empty_result()

	if source_doctype == "Lead":
		_merge(totals, reshape_notes(source_doctype))
		_merge(totals, reshape_assignments(source_doctype))
		_merge(totals, reshape_tasks(source_doctype))
	elif source_doctype == "Prospect":
		_merge(totals, reshape_notes(source_doctype))
		_merge(totals, reshape_assignments(source_doctype))
		_merge(totals, reshape_tasks(source_doctype))
	elif source_doctype == "Opportunity":
		_merge(totals, reshape_opportunity_items())
		_merge(totals, reshape_opportunity_contacts())
		_merge(totals, reshape_opportunity_lost_reasons())
		_merge(totals, reshape_notes(source_doctype))
		_merge(totals, reshape_assignments(source_doctype))
		_merge(totals, reshape_tasks(source_doctype))
		# Dynamic Link repoint runs once on the last source step (after
		# Lead/Prospect have landed their target rows). Covers Contact +
		# Address linkages for all three source doctypes in one pass.
		_merge(totals, reshape_dynamic_links())

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

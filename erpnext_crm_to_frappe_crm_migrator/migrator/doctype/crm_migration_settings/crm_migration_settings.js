// Client script for CRM Migration Settings.
// Frappe auto-loads this file from the doctype folder; no doctype_js hook needed.

const SOURCE_DOCTYPES = [
	"Lead",
	"Opportunity",
	"Prospect",
	"Territory",
	"Industry Type",
	"UTM Source",
	"Opportunity Lost Reason",
	"Item",
];

function dt_to_slug(dt) {
	return dt.toLowerCase().replace(/ /g, "_");
}

function escape_html(s) {
	if (s === null || s === undefined) return "";
	return String(s)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;");
}

function is_locked(frm, source_doctype) {
	return !!frm.doc[`${dt_to_slug(source_doctype)}_locked`];
}

function all_locked(frm) {
	return SOURCE_DOCTYPES.every((dt) => is_locked(frm, dt));
}

function render_mapped_summary(frm, source_doctype) {
	const slug = dt_to_slug(source_doctype);
	const meta_field = `${slug}_mapped_meta`;
	const html_field = `${slug}_mapped_summary`;
	const target = frm.doc[`target_${slug}`] || "";

	const wrapper = frm.fields_dict[html_field] && frm.fields_dict[html_field].$wrapper;
	if (!wrapper) return;

	let mapped = [];
	try {
		mapped = JSON.parse(frm.doc[meta_field] || "[]");
	} catch (e) {
		mapped = [];
	}

	if (!mapped.length) {
		wrapper.html(
			`<div class="text-muted" style="padding:6px 0">` +
				__("No auto-mapped fields yet. Click <b>Refresh Diff</b> to scan source meta.") +
				`</div>`
		);
		return;
	}

	const with_risk = mapped.filter((r) => r.risk).length;
	const rows = mapped
		.map((r) => {
			const tgt = `${escape_html(r.source_field)} → <code>${escape_html(r.target_field)}</code>`;
			const ctype = r.is_custom
				? ` <span class="badge badge-info" style="font-size:10px">custom</span>`
				: "";
			const risk = r.risk
				? `<br><span class="text-warning" style="font-size:11px">⚠ ${escape_html(r.risk)}</span>`
				: "";
			return `<li>${tgt}${ctype}${risk}</li>`;
		})
		.join("");

	const headline =
		`<b>${mapped.length}</b> ` +
		__("auto-mapped to") +
		` <code>${escape_html(target)}</code>` +
		(with_risk
			? ` &middot; <span class="text-warning">${with_risk} ${__("with type-mismatch warnings")}</span>`
			: "");

	wrapper.html(
		`<details style="padding:6px 0">` +
			`<summary style="cursor:pointer">${headline}</summary>` +
			`<ul style="margin-top:8px;padding-left:20px;font-size:13px;line-height:1.6">${rows}</ul>` +
			`</details>`
	);
}

function apply_target_field_options(frm) {
	const cache = frm._target_fields_cache || {};
	SOURCE_DOCTYPES.forEach((dt) => {
		const slug = dt_to_slug(dt);
		const target_dt = frm.doc[`target_${slug}`];
		const fields = cache[target_dt] || [];
		const grid = frm.fields_dict[`${slug}_field_mapping`]
			&& frm.fields_dict[`${slug}_field_mapping`].grid;
		if (!grid) return;
		grid.update_docfield_property("target_field", "options", fields.join("\n"));
	});
}

function lock_one(frm, source_doctype) {
	frappe.confirm(
		__("Freeze the {0} mapping into CRM Migration Field Map? Any previously locked rows for {0} are replaced.", [source_doctype]),
		() => {
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.lock_doctype",
				args: { source_doctype },
				freeze: true,
				freeze_message: __("Locking {0}…", [source_doctype]),
				callback(r) {
					if (!r.message || !r.message.ok) return;
					frappe.show_alert({
						message: __("Locked {0}: {1} fields.", [source_doctype, r.message.inserted]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		}
	);
}

function unlock_one(frm, source_doctype) {
	frappe.confirm(
		__("Unlock {0} so you can refresh and re-lock it? Existing CRM Migration Field Map rows for {0} are kept until the next lock.", [source_doctype]),
		() => {
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.unlock_doctype",
				args: { source_doctype },
				callback(r) {
					if (!r.message || !r.message.ok) return;
					frm.reload_doc();
				},
			});
		}
	);
}

function start_run(frm, source_doctype) {
	const label = source_doctype || __("all source doctypes");
	frappe.confirm(
		__("Start migration for {0}? Runs in the background; watch progress in CRM Migration Run.", [label]),
		() => {
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.runner.run_migration",
				args: { source_doctype: source_doctype || null },
				freeze: true,
				freeze_message: __("Enqueuing migration job…"),
				callback(r) {
					if (!r.message || !r.message.ok) return;
					frappe.show_alert({
						message: __("Migration enqueued: {0}", [r.message.run]),
						indicator: "green",
					});
					frappe.set_route("Form", "CRM Migration Run", r.message.run);
				},
			});
		}
	);
}

frappe.ui.form.on("CRM Migration Settings", {
	refresh(frm) {
		frm.disable_save();

		// --- Refresh Diff (skips locked tabs server-side) ---
		frm.add_custom_button(__("Refresh Diff"), () => {
			frm._target_fields_cache = null;
			frappe.show_alert({ message: __("Scanning ERPNext doctypes…"), indicator: "blue" });
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.refresh_diff",
				freeze: true,
				freeze_message: __("Rebuilding field mapping tables…"),
				callback(r) {
					if (!r.message || !r.message.ok) return;
					const summary = r.message.summary || {};
					const lines = Object.entries(summary).map(([dt, s]) => {
						if (s.locked) return `<li><b>${dt}</b>: <i>locked — left untouched</i></li>`;
						if (s.skipped) return `<li>${dt}: <i>not installed — skipped</i></li>`;
						return (
							`<li><b>${dt}</b>: ${s.rows} fields ` +
							`(${s.map} mapped, ${s.unresolved} unresolved` +
							(s.with_risk ? `, ${s.with_risk} flagged` : "") +
							`) — ${s.row_count} source rows</li>`
						);
					});
					frappe.msgprint({
						title: __("Diff refreshed"),
						message: `<ul style="padding-left:20px">${lines.join("")}</ul>`,
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		}, __("Mapping"));

		// --- Run Migration (top-level): only when ALL tabs are locked ---
		if (all_locked(frm)) {
			frm.add_custom_button(__("Run Migration"), () => start_run(frm, null), __("Migration"))
				.addClass("btn-primary");

			frm.add_custom_button(__("Migrate Activity Records"), () => {
				frappe.confirm(
					__("Rewrite reference_doctype on Comments, ToDos, Notes, etc. that point at ERPNext source doctypes (Lead/Opportunity/Prospect/…) so they point at the Frappe CRM equivalents instead. Reference names are unchanged."),
					() => {
						frappe.call({
							method: "erpnext_crm_to_frappe_crm_migrator.api.runner.run_activity_only",
							freeze: true,
							freeze_message: __("Enqueuing activity rewrite…"),
							callback(r) {
								if (!r.message || !r.message.ok) return;
								frappe.show_alert({
									message: __("Activity rewrite enqueued: {0}", [r.message.run]),
									indicator: "green",
								});
								frappe.set_route("Form", "CRM Migration Run", r.message.run);
							},
						});
					}
				);
			}, __("Migration"));
		}

		// --- Per-tab read-only state for table when its tab is locked ---
		SOURCE_DOCTYPES.forEach((dt) => {
			const slug = dt_to_slug(dt);
			const locked = is_locked(frm, dt);
			frm.set_df_property(`${slug}_field_mapping`, "read_only", locked ? 1 : 0);
		});

		// --- Render the auto-mapped HTML summary on each tab ---
		SOURCE_DOCTYPES.forEach((dt) => render_mapped_summary(frm, dt));

		// --- Per-tab "Mark all as Skip" grid button (only when not locked) ---
		SOURCE_DOCTYPES.forEach((dt) => {
			const slug = dt_to_slug(dt);
			const table_field = `${slug}_field_mapping`;
			const grid = frm.fields_dict[table_field]
				&& frm.fields_dict[table_field].grid;
			if (!grid || grid._skip_button_added) return;
			if (is_locked(frm, dt)) return;
			grid.add_custom_button(__("Mark all as Skip"), () => {
				const rows = frm.doc[table_field] || [];
				if (!rows.length) {
					frappe.show_alert({
						message: __("Nothing to skip — table is empty."),
						indicator: "blue",
					});
					return;
				}
				let touched = 0;
				rows.forEach((r) => {
					if (r.action !== "Skip") {
						r.action = "Skip";
						r.target_field = "";
						r.risk = "";
						touched += 1;
					}
				});
				frm.refresh_field(table_field);
				frm.dirty();
				frappe.show_alert({
					message: __("{0} row(s) marked as Skip.", [touched]),
					indicator: "green",
				});
			});
			grid._skip_button_added = true;
		});

		// --- Populate target_field Autocomplete per tab from target meta ---
		if (!frm._target_fields_cache) {
			frappe.call({
				method:
					"erpnext_crm_to_frappe_crm_migrator.api.mapping.get_target_doctype_fields",
				callback(r) {
					if (!r.message) return;
					frm._target_fields_cache = r.message;
					apply_target_field_options(frm);
				},
			});
		} else {
			apply_target_field_options(frm);
		}
	},

	// --- Per-tab Lock buttons ---
	lead_lock_btn(frm) { lock_one(frm, "Lead"); },
	opportunity_lock_btn(frm) { lock_one(frm, "Opportunity"); },
	prospect_lock_btn(frm) { lock_one(frm, "Prospect"); },
	territory_lock_btn(frm) { lock_one(frm, "Territory"); },
	industry_type_lock_btn(frm) { lock_one(frm, "Industry Type"); },
	utm_source_lock_btn(frm) { lock_one(frm, "UTM Source"); },
	opportunity_lost_reason_lock_btn(frm) { lock_one(frm, "Opportunity Lost Reason"); },
	item_lock_btn(frm) { lock_one(frm, "Item"); },

	// --- Per-tab Unlock buttons ---
	lead_unlock_btn(frm) { unlock_one(frm, "Lead"); },
	opportunity_unlock_btn(frm) { unlock_one(frm, "Opportunity"); },
	prospect_unlock_btn(frm) { unlock_one(frm, "Prospect"); },
	territory_unlock_btn(frm) { unlock_one(frm, "Territory"); },
	industry_type_unlock_btn(frm) { unlock_one(frm, "Industry Type"); },
	utm_source_unlock_btn(frm) { unlock_one(frm, "UTM Source"); },
	opportunity_lost_reason_unlock_btn(frm) { unlock_one(frm, "Opportunity Lost Reason"); },
	item_unlock_btn(frm) { unlock_one(frm, "Item"); },

	// --- Per-tab Migrate buttons ---
	migrate_lead_btn(frm) { start_run(frm, "Lead"); },
	migrate_opportunity_btn(frm) { start_run(frm, "Opportunity"); },
	migrate_prospect_btn(frm) { start_run(frm, "Prospect"); },
	migrate_territory_btn(frm) { start_run(frm, "Territory"); },
	migrate_industry_type_btn(frm) { start_run(frm, "Industry Type"); },
	migrate_utm_source_btn(frm) { start_run(frm, "UTM Source"); },
	migrate_opportunity_lost_reason_btn(frm) { start_run(frm, "Opportunity Lost Reason"); },
	migrate_item_btn(frm) { start_run(frm, "Item"); },
});

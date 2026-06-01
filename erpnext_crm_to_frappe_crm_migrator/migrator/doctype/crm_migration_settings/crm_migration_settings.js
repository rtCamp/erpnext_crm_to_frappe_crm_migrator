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

function any_locked(frm) {
	return SOURCE_DOCTYPES.some((dt) => is_locked(frm, dt));
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
	// Dark amber for warnings — Bootstrap's text-warning is too pale on
	// light backgrounds to read at small sizes.
	const WARN_STYLE = "color:#a04000;font-weight:500";
	const rows = mapped
		.map((r) => {
			const tgt = `${escape_html(r.source_field)} → <code>${escape_html(
				r.target_field
			)}</code>`;
			const ctype = r.is_custom
				? ` <span class="badge badge-info" style="font-size:10px">custom</span>`
				: "";
			const risk = r.risk
				? `<br><span style="${WARN_STYLE};font-size:11px">⚠ ${escape_html(r.risk)}</span>`
				: "";
			return `<li>${tgt}${ctype}${risk}</li>`;
		})
		.join("");

	const headline =
		`<b>${mapped.length}</b> ` +
		__("auto-mapped to") +
		` <code>${escape_html(target)}</code>` +
		(with_risk
			? ` &middot; <span style="${WARN_STYLE}">${with_risk} ${__(
					"with type-mismatch warnings"
			  )}</span>`
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
		const grid =
			frm.fields_dict[`${slug}_field_mapping`] &&
			frm.fields_dict[`${slug}_field_mapping`].grid;
		if (!grid) return;
		grid.update_docfield_property("target_field", "options", fields.join("\n"));
	});
}

function lock_one(frm, source_doctype) {
	frappe.confirm(
		__(
			"Freeze the {0} mapping into CRM Migration Field Map? Any previously locked rows for {0} are replaced.",
			[source_doctype]
		),
		async () => {
			// If the user edited the table (set target_field, flipped action,
			// etc.) without saving, persist it now — the server reads from
			// the DB so unsaved grid mutations would otherwise be lost.
			if (frm.is_dirty()) {
				try {
					await frm.save();
				} catch (e) {
					return; // save error already shown by Frappe
				}
			}
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.lock_doctype",
				args: { source_doctype },
				freeze: true,
				freeze_message: __("Locking {0}…", [source_doctype]),
				callback(r) {
					if (!r.message || !r.message.ok) return;
					frappe.show_alert({
						message: __("Locked {0}: {1} fields.", [
							source_doctype,
							r.message.inserted,
						]),
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
		__(
			"Unlock {0} so you can refresh and re-lock it? Existing CRM Migration Field Map rows for {0} are kept until the next lock.",
			[source_doctype]
		),
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

function unlock_all(frm) {
	const locked = SOURCE_DOCTYPES.filter((dt) => is_locked(frm, dt));
	if (!locked.length) return;
	frappe.confirm(
		__(
			"Unlock every locked tab ({0})? CRM Migration Field Map rows are kept until the next lock.",
			[locked.join(", ")]
		),
		() => {
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.unlock_all_doctypes",
				freeze: true,
				freeze_message: __("Unlocking all tabs…"),
				callback(r) {
					if (!r.message || !r.message.ok) return;
					const unlocked = r.message.unlocked || [];
					frappe.show_alert({
						message: __("Unlocked {0} tab(s): {1}", [
							unlocked.length,
							unlocked.join(", "),
						]),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		}
	);
}

function start_run(frm, source_doctype) {
	const label = source_doctype || __("all source doctypes");
	frappe.confirm(
		__(
			"Start migration for {0}? Runs in the background; watch progress in CRM Migration Run.",
			[label]
		),
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

function start_cleanup(frm) {
	frappe.call({
		method: "erpnext_crm_to_frappe_crm_migrator.api.cleanup.get_cleanup_preview",
		freeze: true,
		freeze_message: __("Scanning source tables…"),
		callback(r) {
			if (!r.message || !r.message.ok) return;
			const m = r.message;

			if (!m.all_locked) {
				frappe.msgprint({
					title: __("Cannot clean up"),
					message: __("Lock every tab first. Unlocked: {0}", [
						m.unlocked_tabs.join(", "),
					]),
					indicator: "red",
				});
				return;
			}
			if (!m.has_successful_run) {
				frappe.msgprint({
					title: __("Cannot clean up"),
					message: __(
						"No successful CRM Migration Run found. Run the migration to completion before cleanup."
					),
					indicator: "red",
				});
				return;
			}

			const parents_html =
				(m.parents || [])
					.map((p) => `<li><b>${escape_html(p.doctype)}</b> — ${p.count} rows</li>`)
					.join("") || `<li class="text-muted">${__("(no source rows found)")}</li>`;

			const children_html =
				(m.children || [])
					.map(
						(c) =>
							`<li><code>tab${escape_html(c.doctype)}</code> — ${c.count} rows ` +
							`(parenttype ∈ ${escape_html(c.parenttypes.join(", "))})</li>`
					)
					.join("") || `<li class="text-muted">${__("(no child rows)")}</li>`;

			const skipped_html = (m.skipped || [])
				.map(
					(s) =>
						`<li><code>${escape_html(s.doctype)}</code> (${
							s.count
						} rows) — ${escape_html(s.reason)}</li>`
				)
				.join("");

			const body =
				`<div style="font-size:13px">` +
				`<p>${__(
					"This will permanently delete the following ERPNext source data. <b>This is irreversible.</b>"
				)}</p>` +
				`<h6 style="margin-top:14px">${__("Parent doctypes")}</h6>` +
				`<ul style="padding-left:18px">${parents_html}</ul>` +
				`<h6 style="margin-top:14px">${__("Child rows (deleted before parents)")}</h6>` +
				`<ul style="padding-left:18px">${children_html}</ul>` +
				`<p class="text-muted" style="margin-top:8px">${__(
					"Contact + Address Dynamic Links were already re-pointed to the CRM-side equivalents during migration, so nothing to delete there."
				)}</p>` +
				`<h6 style="margin-top:14px;color:#1a5490">${__(
					"Left untouched (shared with other ERPNext modules)"
				)}</h6>` +
				`<ul style="padding-left:18px;color:#1a5490">${skipped_html}</ul>` +
				`</div>`;

			const dialog = new frappe.ui.Dialog({
				title: __("Clean up ERPNext source data"),
				size: "large",
				fields: [
					{ fieldtype: "HTML", fieldname: "preview", options: body },
					{
						fieldtype: "Data",
						fieldname: "confirm_text",
						label: __("Type DELETE to confirm"),
						reqd: 1,
					},
				],
				primary_action_label: __("Delete"),
				primary_action({ confirm_text }) {
					if (confirm_text !== "DELETE") {
						frappe.msgprint({
							title: __("Wrong confirmation"),
							message: __("Type the word DELETE exactly to proceed."),
							indicator: "orange",
						});
						return;
					}
					dialog.hide();
					frappe.call({
						method: "erpnext_crm_to_frappe_crm_migrator.api.cleanup.cleanup_source_data",
						args: { confirm: "DELETE" },
						freeze: true,
						freeze_message: __("Deleting source data…"),
						callback(r2) {
							if (!r2.message || !r2.message.ok) return;
							const deleted = r2.message.deleted || {};
							const lines = Object.entries(deleted)
								.map(([dt, n]) => `<li>${escape_html(dt)} — ${n}</li>`)
								.join("");
							frappe.msgprint({
								title: __("Cleanup complete"),
								message: `<ul style="padding-left:18px">${
									lines || "<li>(nothing to delete)</li>"
								}</ul>`,
								indicator: "green",
							});
						},
					});
				},
			});
			dialog.get_primary_btn().addClass("btn-danger");
			dialog.show();
		},
	});
}

function start_undo(frm) {
	frappe.call({
		method: "erpnext_crm_to_frappe_crm_migrator.api.undo.get_undo_preview",
		freeze: true,
		freeze_message: __("Scanning target tables…"),
		callback(r) {
			if (!r.message || !r.message.ok) return;
			const m = r.message;

			if (!m.all_locked) {
				frappe.msgprint({
					title: __("Cannot undo"),
					message: __("Lock every tab first. Unlocked: {0}", [
						m.unlocked_tabs.join(", "),
					]),
					indicator: "red",
				});
				return;
			}
			if (!m.source_rows_remain) {
				frappe.msgprint({
					title: __("Cannot undo"),
					message: __(
						"ERPNext source rows have been cleaned up. Reverting activity references " +
							"now would create orphan references. Undo is only safe before " +
							"<i>Clean up ERPNext source data</i> runs."
					),
					indicator: "red",
				});
				return;
			}

			const marker_html =
				(m.marker_tagged || [])
					.map(
						(x) =>
							`<li><b>${escape_html(x.doctype)}</b> — ${x.count} rows ` +
							`(tagged <code>${escape_html(x.marker)}</code>)</li>`
					)
					.join("") || `<li class="text-muted">${__("(none)")}</li>`;

			const reanchor_html =
				(m.reanchored || [])
					.map(
						(x) =>
							`<li><code>${escape_html(x.doctype)}</code> — ${x.count} rows ` +
							`(parenttype <code>${escape_html(x.from)}</code> → <code>${escape_html(
								x.to
							)}</code>)</li>`
					)
					.join("") || `<li class="text-muted">${__("(none)")}</li>`;

			const reshape_children_html =
				(m.reshape_children || [])
					.map(
						(c) =>
							`<li><code>tab${escape_html(c.doctype)}</code> — ${c.count} rows ` +
							`(parenttype ∈ ${escape_html(c.parenttypes.join(", "))})</li>`
					)
					.join("") || `<li class="text-muted">${__("(none)")}</li>`;

			const parents_html =
				(m.parents || [])
					.map((p) => `<li><b>${escape_html(p.doctype)}</b> — ${p.count} rows</li>`)
					.join("") || `<li class="text-muted">${__("(none)")}</li>`;

			const body =
				`<div style="font-size:13px">` +
				`<p>${__(
					"This will revert the target side back to its pre-migration state. " +
						"ERPNext source data is left untouched (it was preserved by the migrator)."
				)}</p>` +
				`<h6 style="margin-top:14px">${__(
					"Activity references — flip back to ERPNext source"
				)}</h6>` +
				`<p class="text-muted" style="margin-top:0">${__(
					"Comments, ToDos (assignment), Notes, etc. that point at CRM Lead / CRM Deal / … " +
						"will be re-pointed at Lead / Opportunity / …"
				)}</p>` +
				`<h6 style="margin-top:14px">${__("Dynamic Link repoint — flip back")}</h6>` +
				`<p class="text-muted" style="margin-top:0">${__(
					"{0} Contact + Address links pointing at CRM targets",
					[m.dynamic_link_repoints || 0]
				)}</p>` +
				`<h6 style="margin-top:14px">${__(
					"Re-anchored children — parenttype reverted"
				)}</h6>` +
				`<ul style="padding-left:18px">${reanchor_html}</ul>` +
				`<h6 style="margin-top:14px">${__(
					"Marker-tagged synthetic rows — deleted"
				)}</h6>` +
				`<ul style="padding-left:18px">${marker_html}</ul>` +
				`<p class="text-muted" style="margin-top:0">${__(
					"Plus {0} open CRM-side assignment ToDos (synthesised from _assign cache).",
					[m.synth_open_todos || 0]
				)}</p>` +
				`<h6 style="margin-top:14px">${__(
					"Reshape-synthesised children — deleted"
				)}</h6>` +
				`<ul style="padding-left:18px">${reshape_children_html}</ul>` +
				`<h6 style="margin-top:14px">${__("CRM parent target rows — DELETED")}</h6>` +
				`<ul style="padding-left:18px">${parents_html}</ul>` +
				`<p class="text-warning" style="margin-top:8px">${__(
					"<b>This is destructive but reversible:</b> re-running the migration will recreate everything from the still-existing ERPNext source data."
				)}</p>` +
				`</div>`;

			const dialog = new frappe.ui.Dialog({
				title: __("Undo migration"),
				size: "large",
				fields: [
					{ fieldtype: "HTML", fieldname: "preview", options: body },
					{
						fieldtype: "Data",
						fieldname: "confirm_text",
						label: __("Type DELETE to confirm"),
						reqd: 1,
					},
				],
				primary_action_label: __("Undo migration"),
				primary_action({ confirm_text }) {
					if (confirm_text !== "DELETE") {
						frappe.msgprint({
							title: __("Wrong confirmation"),
							message: __("Type the word DELETE exactly to proceed."),
							indicator: "orange",
						});
						return;
					}
					dialog.hide();
					frappe.call({
						method: "erpnext_crm_to_frappe_crm_migrator.api.undo.undo_migration",
						args: { confirm: "DELETE" },
						freeze: true,
						freeze_message: __("Enqueuing undo run…"),
						callback(r2) {
							if (!r2.message || !r2.message.ok) return;
							frappe.show_alert({
								message: __("Undo run enqueued: {0}", [r2.message.run]),
								indicator: "orange",
							});
							frappe.set_route("Form", "CRM Migration Run", r2.message.run);
						},
					});
				},
			});
			dialog.get_primary_btn().addClass("btn-danger");
			dialog.show();
		},
	});
}

function render_migrator_details(frm) {
	const wrapper =
		frm.fields_dict.migration_details_html && frm.fields_dict.migration_details_html.$wrapper;
	if (!wrapper) return;

	wrapper.html(
		`<div class="text-muted" style="padding:6px 0">${__("Loading migrator details…")}</div>`
	);

	frappe.call({
		method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.get_migrator_details",
		callback(r) {
			if (!r.message) return;
			const rows = (r.message.activity_doctypes || [])
				.map((row) => {
					const scope = row.scope
						? `<span class="text-muted" style="font-size:11px">${escape_html(
								row.scope
						  )}</span>`
						: `<span class="text-muted" style="font-size:11px">${__(
								"all rows pointing at a source doctype"
						  )}</span>`;
					return (
						`<tr><td><b>${escape_html(row.doctype)}</b></td>` +
						`<td><code>${escape_html(row.field)}</code></td>` +
						`<td>${scope}</td></tr>`
					);
				})
				.join("");
			const table = rows
				? `<table class="table table-bordered" style="margin-top:8px;font-size:12px">
					<thead><tr>
						<th>${__("DocType")}</th>
						<th>${__("Rewritten Field")}</th>
						<th>${__("Scope")}</th>
					</tr></thead>
					<tbody>${rows}</tbody>
				</table>`
				: `<div class="text-muted">${__("No activity doctypes registered.")}</div>`;

			wrapper.html(
				`<div style="padding:6px 0">
					<h6>${__("Activity rewrite scope")}</h6>
					<div class="text-muted" style="font-size:12px">
						${__(
							"The activity rewrite step changes the listed field on each doctype from ERPNext source values (Lead/Opportunity/Prospect/…) to the corresponding Frappe CRM values. Reference <i>names</i> are preserved during the core records migration, so only the doctype column changes. <b>Version</b> rows are included so the audit trail follows the migrated record."
						)}
					</div>
					${table}
					<h6 style="margin-top:14px">${__("Additional migration side-effects")}</h6>
					<ul class="text-muted" style="font-size:12px;padding-left:18px">
						<li>${__(
							"Native ERPNext notes (CRM Note children) become standalone <b>FCRM Note</b> docs anchored to the migrated CRM record. Any <code>custom_note_attachments</code> child rows are re-anchored to the new note."
						)}</li>
						<li>${__(
							"Content ToDos (those with a real description) are converted to <b>CRM Task</b> rows 1:1 with status mapped (Open → Todo, Closed → Done, Cancelled → Canceled; other statuses pass through)."
						)}</li>
						<li>${__(
							"The <code>_assign</code> JSON cache on each migrated row is regenerated into <b>ToDo</b> rows so the assignment widget on the CRM doc page resolves correctly."
						)}</li>
					</ul>
				</div>`
			);
		},
	});
}

frappe.ui.form.on("CRM Migration Settings", {
	refresh(frm) {
		// Save IS allowed — users edit target_field / action in the
		// per-tab tables and need to persist those edits (Ctrl+S,
		// standard Save toolbar). Lock & Freeze also auto-saves a dirty
		// form before calling the lock endpoint so casual users don't
		// have to remember.

		// --- Refresh Diff (skips locked tabs server-side) ---
		frm.add_custom_button(
			__("Refresh Diff"),
			() => {
				frm._target_fields_cache = null;
				frappe.show_alert({
					message: __("Scanning ERPNext doctypes…"),
					indicator: "blue",
				});
				frappe.call({
					method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.refresh_diff",
					freeze: true,
					freeze_message: __("Rebuilding field mapping tables…"),
					callback(r) {
						if (!r.message || !r.message.ok) return;
						const summary = r.message.summary || {};
						const lines = Object.entries(summary).map(([dt, s]) => {
							if (s.locked)
								return `<li><b>${dt}</b>: <i>locked — left untouched</i></li>`;
							if (s.skipped) return `<li>${dt}: <i>not installed — skipped</i></li>`;
							return (
								`<li><b>${dt}</b>: ${s.rows} fields ` +
								`(${s.map} mapped, ${s.unresolved} need decision` +
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
			},
			__("Mapping")
		);

		// --- Unlock All (only when at least one tab is locked) ---
		if (any_locked(frm)) {
			frm.add_custom_button(__("Unlock All"), () => unlock_all(frm), __("Mapping"));
		}

		// --- Default: Skip All & Migrate (always visible — does locking too) ---
		frm.add_custom_button(
			__("Default: Skip All & Migrate"),
			() => {
				frappe.confirm(
					__(
						"This will: <ol><li>Refresh the diff for every unlocked tab</li>" +
							"<li>Mark every unmapped field as <b>Skip</b></li>" +
							"<li>Lock every unlocked tab</li>" +
							"<li>Run the full migration in the background, including the activity rewrite</li></ol>" +
							"Already-locked tabs are preserved. Continue?"
					),
					() => {
						frappe.call({
							method: "erpnext_crm_to_frappe_crm_migrator.api.runner.default_setup_and_run",
							freeze: true,
							freeze_message: __("Configuring defaults and enqueuing migration…"),
							callback(r) {
								if (!r.message || !r.message.ok) return;
								const m = r.message;
								frappe.show_alert({
									message: __(
										"Locked {0} tab(s) ({1} were already locked); migration enqueued: {2}",
										[m.locked_now, m.already_locked, m.run]
									),
									indicator: "green",
								});
								if (m.run) {
									frappe.set_route("Form", "CRM Migration Run", m.run);
								}
							},
						});
					}
				);
			},
			__("Migration")
		).addClass("btn-primary");

		// --- Run Migration (top-level): only when ALL tabs are locked ---
		if (all_locked(frm)) {
			frm.add_custom_button(
				__("Run Migration"),
				() => start_run(frm, null),
				__("Migration")
			);

			frm.add_custom_button(
				__("Migrate Activity Records"),
				() => {
					frappe.confirm(
						__(
							"Rewrite reference_doctype on Comments, ToDos, Notes, etc. that point at ERPNext source doctypes (Lead/Opportunity/Prospect/…) so they point at the Frappe CRM equivalents instead. Reference names are unchanged."
						),
						() => {
							frappe.call({
								method: "erpnext_crm_to_frappe_crm_migrator.api.runner.run_activity_only",
								freeze: true,
								freeze_message: __("Enqueuing activity rewrite…"),
								callback(r) {
									if (!r.message || !r.message.ok) return;
									frappe.show_alert({
										message: __("Activity rewrite enqueued: {0}", [
											r.message.run,
										]),
										indicator: "green",
									});
									frappe.set_route("Form", "CRM Migration Run", r.message.run);
								},
							});
						}
					);
				},
				__("Migration")
			);
		}

		// --- Undo migration (destructive, target-side wipe + revert) ---
		// Reverts every target-side write the migrator made: activity
		// refs, dynamic-link repoint, reanchored children, marker-tagged
		// synthetic rows (FCRM Note / CRM Task), reshape children on
		// CRM Deal, and finally the CRM parent target rows. Source
		// ERPNext data is left untouched (it was preserved by the
		// migrator). Refuses if cleanup already deleted the source rows.
		if (all_locked(frm)) {
			frm.add_custom_button(__("Undo migration"), () => {
				start_undo(frm);
			}).addClass("btn-danger");
		}

		// --- Clean up ERPNext source data (destructive, post-migration) ---
		// Top-level red button so users don't fire it by accident from a
		// submenu. Visible only when every tab is locked AND a successful
		// migration run exists — the cleanup endpoint re-checks server-side.
		if (all_locked(frm)) {
			frm.add_custom_button(__("Clean up ERPNext source data"), () => {
				start_cleanup(frm);
			}).addClass("btn-danger");
		}

		// --- Per-tab read-only state for table when its tab is locked ---
		SOURCE_DOCTYPES.forEach((dt) => {
			const slug = dt_to_slug(dt);
			const locked = is_locked(frm, dt);
			frm.set_df_property(`${slug}_field_mapping`, "read_only", locked ? 1 : 0);
		});

		// --- Render the auto-mapped HTML summary on each tab ---
		SOURCE_DOCTYPES.forEach((dt) => render_mapped_summary(frm, dt));

		// --- Render the Details tab (version + activity-rewrite scope) ---
		render_migrator_details(frm);

		// --- Per-tab "Mark all as Skip" grid button (only when not locked) ---
		// Hits a server endpoint so the change is persisted in one round-trip
		// — the form has disable_save() so client-side mutations have no
		// save path otherwise, and Lock would still see the old blanks.
		SOURCE_DOCTYPES.forEach((dt) => {
			const slug = dt_to_slug(dt);
			const table_field = `${slug}_field_mapping`;
			const grid = frm.fields_dict[table_field] && frm.fields_dict[table_field].grid;
			if (!grid || grid._skip_button_added) return;
			if (is_locked(frm, dt)) return;
			grid.add_custom_button(__("Mark all as Skip"), () => {
				frappe.call({
					method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.mark_all_as_skip",
					args: { source_doctype: dt },
					freeze: true,
					freeze_message: __("Marking all as Skip…"),
					callback(r) {
						if (!r.message || !r.message.ok) return;
						const touched = r.message.touched || 0;
						if (!touched) {
							frappe.show_alert({
								message: __("Nothing to skip — table is empty or already done."),
								indicator: "blue",
							});
						} else {
							frappe.show_alert({
								message: __("{0} row(s) marked as Skip.", [touched]),
								indicator: "green",
							});
						}
						frm.reload_doc();
					},
				});
			});
			grid._skip_button_added = true;
		});

		// --- Populate target_field Autocomplete per tab from target meta ---
		if (!frm._target_fields_cache) {
			frappe.call({
				method: "erpnext_crm_to_frappe_crm_migrator.api.mapping.get_target_doctype_fields",
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
	lead_lock_btn(frm) {
		lock_one(frm, "Lead");
	},
	opportunity_lock_btn(frm) {
		lock_one(frm, "Opportunity");
	},
	prospect_lock_btn(frm) {
		lock_one(frm, "Prospect");
	},
	territory_lock_btn(frm) {
		lock_one(frm, "Territory");
	},
	industry_type_lock_btn(frm) {
		lock_one(frm, "Industry Type");
	},
	utm_source_lock_btn(frm) {
		lock_one(frm, "UTM Source");
	},
	opportunity_lost_reason_lock_btn(frm) {
		lock_one(frm, "Opportunity Lost Reason");
	},
	item_lock_btn(frm) {
		lock_one(frm, "Item");
	},

	// --- Per-tab Unlock buttons ---
	lead_unlock_btn(frm) {
		unlock_one(frm, "Lead");
	},
	opportunity_unlock_btn(frm) {
		unlock_one(frm, "Opportunity");
	},
	prospect_unlock_btn(frm) {
		unlock_one(frm, "Prospect");
	},
	territory_unlock_btn(frm) {
		unlock_one(frm, "Territory");
	},
	industry_type_unlock_btn(frm) {
		unlock_one(frm, "Industry Type");
	},
	utm_source_unlock_btn(frm) {
		unlock_one(frm, "UTM Source");
	},
	opportunity_lost_reason_unlock_btn(frm) {
		unlock_one(frm, "Opportunity Lost Reason");
	},
	item_unlock_btn(frm) {
		unlock_one(frm, "Item");
	},

	// --- Per-tab Migrate buttons ---
	migrate_lead_btn(frm) {
		start_run(frm, "Lead");
	},
	migrate_opportunity_btn(frm) {
		start_run(frm, "Opportunity");
	},
	migrate_prospect_btn(frm) {
		start_run(frm, "Prospect");
	},
	migrate_territory_btn(frm) {
		start_run(frm, "Territory");
	},
	migrate_industry_type_btn(frm) {
		start_run(frm, "Industry Type");
	},
	migrate_utm_source_btn(frm) {
		start_run(frm, "UTM Source");
	},
	migrate_opportunity_lost_reason_btn(frm) {
		start_run(frm, "Opportunity Lost Reason");
	},
	migrate_item_btn(frm) {
		start_run(frm, "Item");
	},
});

// Migration Run is a system-produced log — users only ever read it.
// Lock the form so saves and field edits aren't possible from the desk.
frappe.ui.form.on("CRM Migration Run", {
	refresh(frm) {
		frm.disable_save();
		frm.disable_form();
	},
});

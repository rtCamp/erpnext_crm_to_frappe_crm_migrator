"""Field-mapping registry for the ERPNext CRM → Frappe CRM migrator.

The registry only describes mappings between fields that exist natively
on both sides — vanilla ERPNext (Lead / Opportunity / Prospect / lookup
masters) and vanilla Frappe CRM (CRM Lead / CRM Deal / CRM Organization
/ lookup masters).

Each pair has two complementary dicts:
  CRM_X_TO_Y       — CRM field → ERPNext field (used historically)
  Y_TO_CRM_X       — ERPNext field → CRM field (the migration direction)
The runner consumes the second form via SUGGESTION_MAP.
"""

# CRM Lead field name → ERPNext Lead field name
CRM_LEAD_TO_LEAD = {
	"email": "email_id",
	"organization": "company_name",
	"source": "utm_source",
	"first_name": "first_name",
	"middle_name": "middle_name",
	"last_name": "last_name",
	"lead_name": "lead_name",
	"salutation": "salutation",
	"gender": "gender",
	"mobile_no": "mobile_no",
	"phone": "phone",
	"website": "website",
	"job_title": "job_title",
	"lead_owner": "lead_owner",
	"no_of_employees": "no_of_employees",
	"annual_revenue": "annual_revenue",
	"image": "image",
	"territory": "territory",
	"industry": "industry",
	"status": "status",
	# SLA response tracking — rtcamp's ERPNext-side custom fields land on
	# native CRM Lead columns instead of being mirrored as customs.
	"response_by": "custom_last_response_by",
	"last_responded_on": "custom_last_responded_on",
}
LEAD_TO_CRM_LEAD = {v: k for k, v in CRM_LEAD_TO_LEAD.items()}

# CRM Deal → Opportunity
CRM_DEAL_TO_OPPORTUNITY = {
	"organization": "party_name",
	"deal_owner": "opportunity_owner",
	"deal_value": "opportunity_amount",
	"expected_closure_date": "expected_closing",
	"contact": "contact_person",
	"email": "contact_email",
	"source": "utm_source",
	"status": "status",
	"closed_date": "custom_won_date",
	"organization_name": "customer_name",
	"exchange_rate": "conversion_rate",
	"probability": "probability",
	"currency": "currency",
	"annual_revenue": "annual_revenue",
	"no_of_employees": "no_of_employees",
	"industry": "industry",
	"territory": "territory",
	"website": "website",
	"phone": "phone",
	"job_title": "job_title",
	"contact_email": "contact_email",
	# SLA response tracking — rtcamp's ERPNext-side custom fields land on
	# native CRM Deal columns instead of being mirrored as customs.
	"response_by": "custom_last_response_by",
	"last_responded_on": "custom_last_responded_on",
}
OPPORTUNITY_TO_CRM_DEAL = {v: k for k, v in CRM_DEAL_TO_OPPORTUNITY.items()}

# CRM Organization → Prospect
CRM_ORG_TO_PROSPECT = {
	"organization_name": "company_name",
	"website": "website",
	"annual_revenue": "annual_revenue",
	"no_of_employees": "no_of_employees",
	"territory": "territory",
	"industry": "industry",
}
PROSPECT_TO_CRM_ORG = {v: k for k, v in CRM_ORG_TO_PROSPECT.items()}

# CRM Territory → Territory
CRM_TERRITORY_TO_TERRITORY = {
	"territory_name": "territory_name",
	"parent_crm_territory": "parent_territory",
	"is_group": "is_group",
}
TERRITORY_TO_CRM_TERRITORY = {v: k for k, v in CRM_TERRITORY_TO_TERRITORY.items()}

# CRM Industry → Industry Type
CRM_INDUSTRY_TO_INDUSTRY_TYPE = {
	"industry": "industry",
}
INDUSTRY_TYPE_TO_CRM_INDUSTRY = {v: k for k, v in CRM_INDUSTRY_TO_INDUSTRY_TYPE.items()}

# CRM Lead Source → UTM Source. Doc `name` is preserved via source-meta;
# additionally written to `source_name` (the user-facing label column on
# CRM Lead Source). `description` text maps to CRM Lead Source.details.
CRM_LEAD_SOURCE_TO_UTM_SOURCE = {
	"source_name": "name",
	"details": "description",
}
UTM_SOURCE_TO_CRM_LEAD_SOURCE = {v: k for k, v in CRM_LEAD_SOURCE_TO_UTM_SOURCE.items()}

# CRM Lost Reason → Opportunity Lost Reason
CRM_LOST_REASON_TO_OPP_LOST_REASON = {
	"lost_reason": "lost_reason",
}
OPP_LOST_REASON_TO_CRM_LOST_REASON = {v: k for k, v in CRM_LOST_REASON_TO_OPP_LOST_REASON.items()}

# CRM Product → Item
CRM_PRODUCT_TO_ITEM = {
	"product_code": "item_code",
	"product_name": "item_name",
	"disabled": "disabled",
	"image": "image",
	"description": "description",
	"standard_rate": "standard_rate",
}
ITEM_TO_CRM_PRODUCT = {v: k for k, v in CRM_PRODUCT_TO_ITEM.items()}

# CRM doctype → ERPNext doctype
DOCTYPE_MAP = {
	"CRM Lead": "Lead",
	"CRM Deal": "Opportunity",
	"CRM Organization": "Prospect",
	"CRM Territory": "Territory",
	"CRM Industry": "Industry Type",
	"CRM Lead Source": "UTM Source",
	"CRM Lost Reason": "Opportunity Lost Reason",
	"CRM Product": "Item",
}
REVERSE_DOCTYPE_MAP = {v: k for k, v in DOCTYPE_MAP.items()}

# Ordered list of source doctypes the migrator handles. UI-tab order on
# the Settings form. The runner's MIGRATION_ORDER (defined separately)
# uses dependency order: lookups before parents.
SOURCE_DOCTYPES = [
	"Lead",
	"Opportunity",
	"Prospect",
	"Territory",
	"Industry Type",
	"UTM Source",
	"Opportunity Lost Reason",
	"Item",
]

# ERPNext source doctype → suggestion dict (source_field → target_field on CRM side)
SUGGESTION_MAP = {
	"Lead": LEAD_TO_CRM_LEAD,
	"Opportunity": OPPORTUNITY_TO_CRM_DEAL,
	"Prospect": PROSPECT_TO_CRM_ORG,
	"Territory": TERRITORY_TO_CRM_TERRITORY,
	"Industry Type": INDUSTRY_TYPE_TO_CRM_INDUSTRY,
	"UTM Source": UTM_SOURCE_TO_CRM_LEAD_SOURCE,
	"Opportunity Lost Reason": OPP_LOST_REASON_TO_CRM_LOST_REASON,
	"Item": ITEM_TO_CRM_PRODUCT,
}

# Frappe metadata fields — never surfaced in the field-mapping table.
FRAMEWORK_FIELDS = {
	"name",
	"creation",
	"modified",
	"modified_by",
	"owner",
	"docstatus",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
}

# Fields explicitly skipped — ERPNext-only naming scaffolding with no CRM equivalent.
SKIP_FIELDS = {
	"naming_series",
	"erpnext_lead",
}

ALL_SKIP_FIELDS = FRAMEWORK_FIELDS | SKIP_FIELDS

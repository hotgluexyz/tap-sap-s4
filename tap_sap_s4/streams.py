"""Standard entity sets, with schemas and keys loaded from captured SAP EDMX."""

from tap_sap_s4.client import SapS4Stream
from tap_sap_s4.schema import entity_schema


class CompanyCodesStream(SapS4Stream):
    name = "company_codes"
    path = "/API_COMPANYCODE_SRV/A_CompanyCode"
    schema, primary_keys = entity_schema("company.xml", "A_CompanyCode")


class GlAccountsStream(SapS4Stream):
    name = "gl_accounts"
    path = "/API_GLACCOUNTINCHARTOFACCOUNTS_SRV/A_GLAccountInChartOfAccounts"
    schema, primary_keys = entity_schema("gl.xml", "A_GLAccountInChartOfAccounts")


class GlAccountTextsStream(SapS4Stream):
    name = "gl_account_texts"
    path = "/API_GLACCOUNTINCHARTOFACCOUNTS_SRV/A_GLAccountText"
    schema, primary_keys = entity_schema("gl.xml", "A_GLAccountText")


class JournalEntryItemsStream(SapS4Stream):
    name = "journal_entry_items"
    path = "/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    schema, primary_keys = entity_schema("items.xml", "A_OperationalAcctgDocItemCube")

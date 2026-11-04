"""SAP S/4HANA OData V2 Singer tap."""

from typing import ClassVar

from hotglue_singer_sdk import Tap

from tap_sap_s4.client import validate_origin
from tap_sap_s4.streams import (
    CompanyCodesStream,
    GlAccountsStream,
    GlAccountTextsStream,
    JournalEntryItemsStream,
)

STREAM_TYPES = [CompanyCodesStream, GlAccountsStream, GlAccountTextsStream, JournalEntryItemsStream]


class TapSapS4(Tap):
    name = "tap-sap-s4"
    config_jsonschema: ClassVar[dict] = {
        "type": "object",
        "required": ["base_url", "username", "password"],
        "properties": {
            "base_url": {"type": "string", "description": "HTTPS origin of the SAP system"},
            "username": {"type": "string", "minLength": 1},
            "password": {"type": "string", "minLength": 1, "writeOnly": True},
            "sap_client": {"type": "string"},
            "company_code": {"type": "string"},
            "verify_ssl": {"type": "boolean", "default": True},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        validate_origin(self.config["base_url"])

    def discover_streams(self):
        return [stream_class(tap=self) for stream_class in STREAM_TYPES]


if __name__ == "__main__":
    TapSapS4.cli()

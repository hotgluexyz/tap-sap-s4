"""Convert captured SAP metadata into lossless Singer schemas, without network discovery."""

from importlib.resources import files
from xml.etree import ElementTree

SAP = "{http://www.sap.com/Protocols/SAPData}"


def entity_schema(filename, entity_set):
    root = ElementTree.fromstring(files("tap_sap_s4").joinpath("metadata", filename).read_bytes())
    entity = next(e for e in root.findall(".//{*}EntitySet") if e.attrib["Name"] == entity_set)
    type_name = entity.attrib["EntityType"].rsplit(".", 1)[-1]
    model = next(e for e in root.findall(".//{*}EntityType") if e.attrib["Name"] == type_name)
    keys = [e.attrib["Name"] for e in model.findall("{*}Key/{*}PropertyRef")]
    properties = {}
    types = {
        "Edm.String": "string",
        "Edm.Boolean": "boolean",
        "Edm.Decimal": "string",
        "Edm.DateTime": "string",
        "Edm.DateTimeOffset": "string",
        "Edm.Time": "string",
    }
    for prop in model.findall("{*}Property"):
        edm = prop.attrib["Type"]
        spec = {
            "type": [types[edm], "null"],
            "description": prop.attrib.get(SAP + "label", prop.attrib["Name"]),
        }
        if edm in ("Edm.DateTime", "Edm.DateTimeOffset"):
            spec["format"] = (
                "date" if prop.attrib.get(SAP + "display-format") == "Date" else "date-time"
            )
        if edm == "Edm.Decimal":
            spec["description"] += " (OData decimal string; preserve precision.)"
        properties[prop.attrib["Name"]] = spec
    return {"type": "object", "properties": properties, "additionalProperties": False}, keys

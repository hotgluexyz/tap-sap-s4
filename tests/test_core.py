"""Exercise public Singer interfaces and actual HTTPS transport without SAP credentials."""

import json
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest
import requests
from hotglue_singer_sdk.exceptions import ConfigValidationError, FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.testing import get_standard_tap_tests

from tap_sap_s4.tap import STREAM_TYPES, TapSapS4

CONFIG = {"base_url": "https://sap.example.com", "username": "test", "password": "test"}
KEYS = {
    "company_codes": ["CompanyCode"],
    "gl_accounts": ["ChartOfAccounts", "GLAccount"],
    "gl_account_texts": ["ChartOfAccounts", "GLAccount", "Language"],
    "journal_entry_items": [
        "CompanyCode",
        "FiscalYear",
        "AccountingDocument",
        "AccountingDocumentItem",
    ],
}


@pytest.mark.parametrize(
    "test_func",
    [
        test
        for test in get_standard_tap_tests(TapSapS4, config=CONFIG.copy())
        if test.__name__ != "_test_stream_connections"
    ],
)
def test_standard(test_func):
    test_func()


@pytest.fixture
def server(tmp_path):
    """An ephemeral self-signed HTTPS OData server; no insecure production switch."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    calls = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            status, headers, body = responses.pop(0)
            payload = json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{httpd.server_port}", responses, calls
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def stream_for(server, name="company_codes", **config):
    return TapSapS4(
        config={**CONFIG, "base_url": server[0], "verify_ssl": False, **config}
    ).streams[name]


def test_real_metadata_catalog():
    tap = TapSapS4(config=CONFIG.copy())
    catalog = tap.catalog.to_dict()
    counts = {
        "company_codes": 21,
        "gl_accounts": 19,
        "gl_account_texts": 6,
        "journal_entry_items": 270,
    }
    assert {s["stream"] for s in catalog["streams"]} == set(KEYS)
    for s in catalog["streams"]:
        name = s["stream"]
        assert s["key_properties"] == KEYS[name]
        assert len(s["schema"]["properties"]) == counts[name]
        assert tap.streams[name].replication_method == "FULL_TABLE"
        assert tap.streams[name].replication_key is None
        for key in KEYS[name]:
            assert s["schema"]["properties"][key]["type"] == ["string", "null"]
    items = tap.streams["journal_entry_items"].schema["properties"]
    assert items["AmountInCompanyCodeCurrency"]["type"] == ["string", "null"]
    assert items["PostingDate"]["format"] == "date"
    assert items["CreationTime"]["type"] == ["string", "null"]
    assert (
        tap.streams["gl_accounts"].schema["properties"]["LastChangeDateTime"]["format"]
        == "date-time"
    )


def test_incremental_catalog_rejected():
    catalog = TapSapS4(config=CONFIG.copy()).catalog.to_dict()
    catalog["streams"][0]["replication_method"] = "INCREMENTAL"
    catalog["streams"][0]["replication_key"] = "LastChangeDateTime"
    with pytest.raises(ValueError, match="FULL_TABLE only"):
        _ = TapSapS4(config=CONFIG.copy(), catalog=catalog).streams


@pytest.mark.parametrize(
    "url",
    [
        "http://sap.example.com",
        "https://user:pass@sap.example.com",
        "https://sap.example.com/api",
        "https://sap.example.com?x=1",
        "https://sap.example.com#frag",
        "https://sap.example.com?",
        "https://sap.example.com#",
        "https:///",
        "https://sap.example.com:bad",
        " https://sap.example.com",
        "https://sap.example.com\\evil",
        "https://sap.example.com\n",
    ],
)
def test_invalid_base_url(url):
    with pytest.raises(ValueError):
        TapSapS4(config={**CONFIG, "base_url": url})


@pytest.mark.parametrize(
    "setting,value",
    [("sap_client", 100), ("company_code", 1000), ("verify_ssl", "false"), ("page_size", 0)],
)
def test_config_types(setting, value):
    with pytest.raises(ConfigValidationError):
        TapSapS4(config={**CONFIG, setting: value})


@pytest.mark.parametrize("link", ["?$skiptoken=abc%2B123", "A_CompanyCode?$skiptoken=abc%2B123"])
def test_https_pagination_and_query_preservation(server, link):
    _, responses, calls = server
    responses.extend(
        [
            (200, {}, {"d": {"results": [{"CompanyCode": "0010"}], "__next": link}}),
            (200, {}, {"d": {"results": [{"CompanyCode": "0020"}]}}),
        ]
    )
    stream = stream_for(server, sap_client="001", company_code="00'1", page_size=17)
    rows = list(stream.get_records(None))
    assert [row["CompanyCode"] for row in rows] == ["0010", "0020"]
    assert len(calls) == 2
    for path, headers in calls:
        params = parse_qs(urlsplit(path).query)
        assert params["$format"] == ["json"]
        assert params["sap-client"] == ["001"]
        assert params["$filter"] == ["CompanyCode eq '00''1'"]
        assert "$top" not in params  # Do not silently cap full-table extraction.
        assert headers["Authorization"] == "Basic dGVzdDp0ZXN0"
        assert headers["Prefer"] == "odata.maxpagesize=17"
    assert parse_qs(urlsplit(calls[1][0]).query)["$skiptoken"] == ["abc+123"]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/items",
        "//evil.example/items",
        "http://sap.example.com/items",
        "https://user:pass@sap.example.com/items",
        "https://sap.example.com:444/items",
        "https://sap.example.com/items#fragment",
    ],
)
def test_no_auth_to_unsafe_next_link(server, link):
    server[1].append((200, {}, {"d": {"results": [], "__next": link}}))
    with pytest.raises(ValueError):
        list(stream_for(server).get_records(None))
    assert len(server[2]) == 1  # Not even an attempted request to the second origin.


def test_absolute_same_origin_and_custom_query(server):
    stream = stream_for(server)
    response = requests.Response()
    response.url = server[0] + stream.path + "?$select=CompanyCode&sap-client=001&$filter=x"
    response._content = json.dumps(
        {"d": {"__next": server[0] + stream.path + "?$skiptoken=next"}}
    ).encode()
    token = stream.get_next_page_token(response, None)
    assert parse_qs(urlsplit(token).query) == {
        "$select": ["CompanyCode"],
        "sap-client": ["001"],
        "$filter": ["x"],
        "$skiptoken": ["next"],
    }


@pytest.mark.parametrize("name", [s.name for s in STREAM_TYPES])
def test_optional_company_filter(name):
    plain = TapSapS4(config=CONFIG.copy()).streams[name].prepare_request(None, None)
    filtered = (
        TapSapS4(config={**CONFIG, "company_code": "0010"})
        .streams[name]
        .prepare_request(None, None)
    )
    assert "$filter" not in parse_qs(urlsplit(plain.url).query)
    assert ("$filter" in parse_qs(urlsplit(filtered.url).query)) == (
        name in ("company_codes", "journal_entry_items")
    )


def test_string_and_date_conversion():
    streams = TapSapS4(config=CONFIG.copy()).streams
    row = streams["journal_entry_items"].post_process(
        {
            "CompanyCode": "0010",
            "FiscalYear": "2026",
            "AccountingDocument": "0000001234",
            "AccountingDocumentItem": "001",
            "GLAccount": "0000400000",
            "AmountInCompanyCodeCurrency": "123456789012345.67",
            "CreationTime": "PT12H30M00S",
            "PostingDate": "/Date(0)/",
            "ClearingDate": None,
            "DocumentDate": "/Date(-86400000)/",
            "__metadata": {"uri": "do-not-emit"},
        }
    )
    assert row["PostingDate"] == "1970-01-01"
    assert row["DocumentDate"] == "1969-12-31"
    assert row["ClearingDate"] is None
    assert row["AmountInCompanyCodeCurrency"] == "123456789012345.67"
    assert row["GLAccount"] == "0000400000"
    assert "__metadata" not in row
    jsonschema.validate(row, streams["journal_entry_items"].schema)
    row = streams["gl_accounts"].post_process(
        {
            "CreationDate": "/Date(0+0200)/",
            "LastChangeDateTime": "/Date(1234-0500)/",
        }
    )
    assert row == {
        "CreationDate": "1970-01-01",
        "LastChangeDateTime": "1970-01-01T00:00:01.234000Z",
    }
    jsonschema.validate(row, streams["gl_accounts"].schema)
    assert (
        streams["gl_accounts"].post_process({"LastChangeDateTime": "2026-01-01T01:00:00+01:00"})[
            "LastChangeDateTime"
        ]
        == "2026-01-01T00:00:00Z"
    )


def test_redirect_not_followed(server):
    server[1].append((302, {"Location": "https://evil.example/"}, {}))
    with pytest.raises(FatalAPIError, match="redirects"):
        list(stream_for(server).get_records(None))
    assert len(server[2]) == 1


def test_verify_ssl_default_rejects_self_signed(server):
    stream = stream_for(server, verify_ssl=True)
    with pytest.raises(requests.exceptions.SSLError):
        stream._request(stream.prepare_request(None, None), None)
    assert server[2] == []


@pytest.mark.parametrize("retry_after", ["7", "date", "invalid"])
def test_bounded_429_retry_after(server, monkeypatch, retry_after):
    if retry_after == "date":
        retry_after = format_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True
        )
    server[1].extend([(429, {"Retry-After": retry_after}, {})] * 5)
    sleeps = []
    monkeypatch.setattr("backoff._sync.time.sleep", sleeps.append)
    with pytest.raises(RetriableAPIError):
        list(stream_for(server).get_records(None))
    assert len(server[2]) == 5
    assert len(sleeps) == 4
    if retry_after == "7":
        assert all(wait >= 7 for wait in sleeps)
    elif retry_after != "invalid":
        assert all(wait >= 25 for wait in sleeps)


@pytest.mark.parametrize("name", list(KEYS))
def test_cli_discovery_and_selected_singer_sync(server, tmp_path, name):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**CONFIG, "base_url": server[0], "verify_ssl": False}))
    args = [sys.executable, "-m", "tap_sap_s4", "--config", str(config)]
    discovery = subprocess.run(args + ["--discover"], check=True, capture_output=True, text=True)
    catalog = json.loads(discovery.stdout)
    assert len(catalog["streams"]) == 4
    assert server[2] == []  # Discovery must not issue HTTP.
    for stream in catalog["streams"]:
        for meta in stream["metadata"]:
            meta["metadata"]["selected"] = stream["stream"] == name
    selected = tmp_path / "catalog.json"
    selected.write_text(json.dumps(catalog))
    row = {key: "0010" for key in KEYS[name]}
    if name == "journal_entry_items":
        row.update(
            {
                "GLAccount": "0000400000",
                "AmountInCompanyCodeCurrency": "123456789012345.67",
                "PostingDate": "/Date(0)/",
            }
        )
    elif name in ("gl_accounts", "gl_account_texts"):
        row["LastChangeDateTime"] = "/Date(0)/"
    server[1].append((200, {}, {"d": {"results": [row]}}))
    sync = subprocess.run(
        args + ["--catalog", str(selected)], check=True, capture_output=True, text=True
    )
    messages = [json.loads(line) for line in sync.stdout.splitlines()]
    schemas = [m for m in messages if m["type"] == "SCHEMA"]
    records = [m for m in messages if m["type"] == "RECORD"]
    assert [m["stream"] for m in schemas] == [name]
    assert schemas[0]["key_properties"] == KEYS[name]
    record = records[0]["record"]
    for key in KEYS[name]:
        assert record[key] == "0010"
    if name == "journal_entry_items":
        assert record["GLAccount"] == "0000400000"
        assert record["AmountInCompanyCodeCurrency"] == "123456789012345.67"
        assert record["PostingDate"] == "1970-01-01"
    elif name in ("gl_accounts", "gl_account_texts"):
        assert record["LastChangeDateTime"] == "1970-01-01T00:00:00Z"
    jsonschema.validate(record, schemas[0]["schema"])
    assert len(server[2]) == 1

"""SAP OData V2 transport: same-origin Basic auth and server-driven paging."""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import backoff
import requests
from hotglue_singer_sdk.exceptions import FatalAPIError, RetriableAPIError
from hotglue_singer_sdk.streams import RESTStream


def validate_origin(value: str) -> str:
    """Require an HTTPS origin, never a credential-bearing URL or API path."""
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or "?" in value
        or "#" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise ValueError("base_url must be an HTTPS origin without credentials, query or fragment")
    _ = url.port  # Validate port syntax before making any requests.
    return value.rstrip("/")


def origin(value: str) -> tuple:
    url = urlsplit(value)
    return url.scheme.lower(), url.hostname, url.port or 443


class SapS4Stream(RESTStream):
    """Full-table SAP entity set; no timestamps are used as bookmarks."""

    replication_key = None
    extra_retry_statuses: ClassVar[list[int]] = [429]
    _retry_after = 0.0

    def apply_catalog(self, catalog):
        entry = catalog.get_stream(self.name)
        if entry and (
            entry.replication_key or entry.replication_method not in (None, "FULL_TABLE")
        ):
            raise ValueError(
                f"{self.name} supports FULL_TABLE only; remove incremental catalog settings"
            )
        super().apply_catalog(catalog)

    @property
    def url_base(self):
        return validate_origin(self.config["base_url"]) + "/sap/opu/odata/sap"

    def get_url_params(self, context, next_page_token):
        # $top is a total-result limit in OData, NOT a page-size hint.
        # Never apply it to a full-table extraction.
        params = {"$format": "json"}
        if self.config.get("sap_client") is not None:
            params["sap-client"] = self.config["sap_client"]
        if (
            self.config.get("company_code") is not None
            and "CompanyCode" in self.schema["properties"]
        ):
            company = self.config["company_code"].replace("'", "''")
            params["$filter"] = f"CompanyCode eq '{company}'"
        return params

    def _safe_url(self, value):
        parsed = urlsplit(value)
        if (
            origin(value) != origin(self.config["base_url"])
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "#" in value
            or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("Refusing unsafe or cross-origin OData pagination URL")
        return value

    def prepare_request(self, context, next_page_token):
        url = self._safe_url(next_page_token or self.get_url(context))
        # __next may only include a skiptoken. Keep format, filter and client.
        parts = urlsplit(url)
        params = self.get_url_params(context, None)
        params.update(parse_qsl(parts.query, keep_blank_values=True))
        url = urlunsplit(parts._replace(query=urlencode(params)))
        request = requests.Request(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "Prefer": f"odata.maxpagesize={self.config.get('page_size', 1000)}",
            },
            auth=(self.config["username"], self.config["password"]),
        )
        return self.requests_session.prepare_request(request)

    def _request(self, prepared_request, context):
        self._safe_url(prepared_request.url)
        response = self.requests_session.send(
            prepared_request,
            timeout=self.timeout,
            allow_redirects=False,
            verify=self.config.get("verify_ssl", True),
        )
        self._retry_after = 0.0
        if response.status_code == 429:
            value = response.headers.get("Retry-After", "")
            try:
                self._retry_after = max(0.0, float(value))
            except ValueError:
                try:
                    self._retry_after = max(
                        0.0,
                        (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
        if 300 <= response.status_code < 400:
            raise FatalAPIError("SAP redirects are disabled; configure the final HTTPS origin")
        self.validate_response(response)
        return response

    def backoff_wait_generator(self):
        for attempt in range(5):
            yield max(self._retry_after, random.uniform(0, min(60, 2 ** (attempt + 1))))

    def request_decorator(self, func):
        # SDK retry hook, without the default jitter that would shorten Retry-After.
        return backoff.on_exception(
            self.backoff_wait_generator,
            (
                RetriableAPIError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
            ),
            max_tries=5,
            jitter=None,
            on_backoff=self.backoff_handler,
        )(func)

    def backoff_handler(self, details):
        self.logger.warning(
            "Retrying SAP request after %.1fs (attempt %s/5)", details["wait"], details["tries"]
        )

    def get_next_page_token(self, response, previous_token):
        next_url = response.json()["d"].get("__next")
        if not next_url:
            return None
        resolved = self._safe_url(urljoin(response.url, next_url))
        # Retain *all* original query options, not only the built-in parameters.
        parts = urlsplit(resolved)
        params = dict(parse_qsl(urlsplit(response.url).query, keep_blank_values=True))
        params.update(parse_qsl(parts.query, keep_blank_values=True))
        return urlunsplit(parts._replace(query=urlencode(params)))

    def parse_response(self, response):
        rows = response.json()["d"]["results"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Expected an OData V2 d.results array of objects")
        yield from rows

    def post_process(self, row, context=None):
        result = {}
        for key, spec in self.schema["properties"].items():
            if key not in row:
                continue
            value = row[key]
            if value is not None and spec.get("format") in ("date", "date-time"):
                match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", value)
                if match:
                    # OData milliseconds represent UTC; the optional suffix is not an offset to add.
                    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                        milliseconds=int(match[1])
                    )
                else:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    dt = dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
                value = (
                    dt.date().isoformat()
                    if spec["format"] == "date"
                    else dt.isoformat().replace("+00:00", "Z")
                )
            result[key] = value
        return result

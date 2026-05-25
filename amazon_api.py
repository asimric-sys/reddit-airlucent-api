"""Amazon Product Advertising API v5 client.

Provides AmazonCreatorsAPI — a thin wrapper around the Amazon PA API v5 that
signs requests with AWS Signature V4 (via boto3 / requests-aws4auth) and
returns product images, prices, and customer review ratings as plain dicts.

Credentials are read from environment variables:
    AMAZON_ACCESS_KEY    — AWS access key ID
    AMAZON_SECRET_KEY    — AWS secret access key
    AMAZON_ASSOCIATE_TAG — Amazon Associates partner tag (e.g. "mytag-20")

Usage example
-------------
    from amazon_api import AmazonCreatorsAPI

    api = AmazonCreatorsAPI(
        access_key=os.getenv("AMAZON_ACCESS_KEY"),
        secret_key=os.getenv("AMAZON_SECRET_KEY"),
        associate_tag=os.getenv("AMAZON_ASSOCIATE_TAG"),
    )

    # Look up a product by ASIN
    product = api.get_product_by_asin("B07RFSSQ7L")

    # Search by keywords (optionally narrow by brand)
    results = api.get_product_by_search("Coway Airmega 400", brand="Coway")
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Amazon PA API v5 constants
# ---------------------------------------------------------------------------
_SERVICE = "ProductAdvertisingAPI"
_VERSION = "paapi5"
_API_VERSION = "2019-01-01"

# Resources requested for every item lookup / search
_ITEM_RESOURCES = [
    "Images.Primary.Large",
    "Images.Primary.Medium",
    "Images.Primary.Small",
    "ItemInfo.Title",
    "ItemInfo.ByLineInfo",
    "Offers.Listings.Price",
    "CustomerReviews.Count",
    "CustomerReviews.StarRating",
]


# ---------------------------------------------------------------------------
# Minimal AWS Signature V4 implementation (no extra dependencies beyond
# the standard library + requests).  boto3 / requests-aws4auth are available
# in the environment but we implement signing inline so the class is
# self-contained and easy to test without real AWS credentials.
# ---------------------------------------------------------------------------

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def _build_auth_header(
    access_key: str,
    secret_key: str,
    region: str,
    host: str,
    method: str,
    uri: str,
    payload: str,
    amz_target: str,
    amz_date: str,
    date_stamp: str,
) -> dict[str, str]:
    """Return the Authorization and x-amz-date headers for a signed request."""
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"content-type:application/json; charset=utf-8\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{amz_target}\n"
    )
    signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"

    canonical_request = "\n".join([
        method,
        uri,
        "",  # canonical query string (empty for POST)
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _get_signing_key(secret_key, date_stamp, region, _SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    return {
        "Authorization": authorization,
        "x-amz-date": amz_date,
    }


# ---------------------------------------------------------------------------
# Public API class
# ---------------------------------------------------------------------------

class AmazonCreatorsAPI:
    """Client for the Amazon Product Advertising API v5.

    Parameters
    ----------
    access_key:
        AWS access key ID.  Defaults to the ``AMAZON_ACCESS_KEY`` env var.
    secret_key:
        AWS secret access key.  Defaults to the ``AMAZON_SECRET_KEY`` env var.
    associate_tag:
        Amazon Associates partner tag (e.g. ``"mytag-20"``).  Defaults to the
        ``AMAZON_ASSOCIATE_TAG`` env var.
    region:
        AWS region for the PA API endpoint.  Defaults to ``"us-east-1"``
        (covers amazon.com).  Use ``"eu-west-1"`` for amazon.co.uk / .de etc.
    """

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        associate_tag: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self.access_key = access_key or os.getenv("AMAZON_ACCESS_KEY", "")
        self.secret_key = secret_key or os.getenv("AMAZON_SECRET_KEY", "")
        self.associate_tag = associate_tag or os.getenv("AMAZON_ASSOCIATE_TAG", "")
        self.region = region

        self._host = f"webservices.amazon.com"
        self._endpoint = f"https://{self._host}/paapi5"

        if not self.access_key or not self.secret_key or not self.associate_tag:
            logger.warning(
                "AmazonCreatorsAPI: one or more credentials are missing "
                "(AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_ASSOCIATE_TAG). "
                "API calls will fail until credentials are provided."
            )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_product_by_asin(self, asin: str) -> dict[str, Any] | None:
        """Fetch product details for a single ASIN.

        Parameters
        ----------
        asin:
            Amazon Standard Identification Number (e.g. ``"B07RFSSQ7L"``).

        Returns
        -------
        dict or None
            Normalised product dict with keys ``asin``, ``title``, ``brand``,
            ``image_url``, ``price``, ``currency``, ``rating``,
            ``ratings_count``, and ``detail_page_url``.
            Returns ``None`` if the request fails or the ASIN is not found.
        """
        payload = {
            "ItemIds": [asin],
            "Resources": _ITEM_RESOURCES,
            "PartnerTag": self.associate_tag,
            "PartnerType": "Associates",
            "Marketplace": "www.amazon.com",
        }

        data = self._post("/getitems", "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems", payload)
        if data is None:
            return None

        items = data.get("ItemsResult", {}).get("Items", [])
        if not items:
            logger.warning("get_product_by_asin: no items returned for ASIN %s", asin)
            return None

        return self._parse_item(items[0])

    def get_product_by_search(
        self,
        keywords: str,
        brand: str | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Search for products by keywords, optionally filtered by brand.

        Parameters
        ----------
        keywords:
            Search terms (e.g. ``"Coway Airmega 400 air purifier"``).
        brand:
            Optional brand name to narrow results (e.g. ``"Coway"``).
        max_results:
            Maximum number of results to return (1–10, Amazon's page limit).

        Returns
        -------
        list[dict]
            List of normalised product dicts (same schema as
            :meth:`get_product_by_asin`).  Empty list on failure.
        """
        max_results = max(1, min(max_results, 10))

        payload: dict[str, Any] = {
            "Keywords": keywords,
            "Resources": _ITEM_RESOURCES,
            "PartnerTag": self.associate_tag,
            "PartnerType": "Associates",
            "Marketplace": "www.amazon.com",
            "ItemCount": max_results,
            "SearchIndex": "All",
        }
        if brand:
            payload["Brand"] = brand

        data = self._post(
            "/searchitems",
            "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
            payload,
        )
        if data is None:
            return []

        items = data.get("SearchResult", {}).get("Items", [])
        if not items:
            logger.info("get_product_by_search: no results for keywords='%s'", keywords)
            return []

        return [self._parse_item(item) for item in items]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, amz_target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Sign and POST a PA API v5 request.

        Parameters
        ----------
        path:
            API path relative to the endpoint (e.g. ``"/getitems"``).
        amz_target:
            Value for the ``x-amz-target`` header.
        payload:
            Request body as a Python dict (will be JSON-serialised).

        Returns
        -------
        dict or None
            Parsed JSON response body, or ``None`` on any error.
        """
        if not self.access_key or not self.secret_key or not self.associate_tag:
            logger.error("_post: credentials not configured — aborting request")
            return None

        body = json.dumps(payload, separators=(",", ":"))
        uri = f"/paapi5{path}"

        now = datetime.now(tz=timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        auth_headers = _build_auth_header(
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            host=self._host,
            method="POST",
            uri=uri,
            payload=body,
            amz_target=amz_target,
            amz_date=amz_date,
            date_stamp=date_stamp,
        )

        headers = {
            "Content-Encoding": "amz-1.0",
            "Content-Type": "application/json; charset=utf-8",
            "Host": self._host,
            "X-Amz-Target": amz_target,
            **auth_headers,
        }

        url = f"{self._endpoint}{path}"
        try:
            resp = requests.post(url, headers=headers, data=body, timeout=15)
        except requests.RequestException as exc:
            logger.error("Amazon PA API request failed (%s %s): %s", "POST", url, exc)
            return None

        if resp.status_code != 200:
            logger.warning(
                "Amazon PA API returned HTTP %s for %s: %s",
                resp.status_code,
                url,
                resp.text[:300],
            )
            return None

        try:
            return resp.json()
        except ValueError as exc:
            logger.error("Failed to parse Amazon PA API response as JSON: %s", exc)
            return None

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> dict[str, Any]:
        """Normalise a raw PA API item dict into a flat product dict.

        Extracts the following fields (all optional — missing fields are
        returned as ``None``):

        * ``asin``            — Amazon ASIN
        * ``title``           — product title
        * ``brand``           — manufacturer / brand name
        * ``image_url``       — URL of the largest available primary image
        * ``price``           — numeric price (float) in the listing currency
        * ``currency``        — ISO 4217 currency code (e.g. ``"USD"``)
        * ``rating``          — average customer star rating (float, 0–5)
        * ``ratings_count``   — total number of customer ratings (int)
        * ``detail_page_url`` — canonical Amazon product page URL
        """
        asin = item.get("ASIN")
        detail_page_url = item.get("DetailPageURL")

        # --- Title ---
        title = (
            item.get("ItemInfo", {})
            .get("Title", {})
            .get("DisplayValue")
        )

        # --- Brand ---
        brand = (
            item.get("ItemInfo", {})
            .get("ByLineInfo", {})
            .get("Brand", {})
            .get("DisplayValue")
        )

        # --- Image: prefer Large, fall back to Medium then Small ---
        image_url: str | None = None
        primary = item.get("Images", {}).get("Primary", {})
        for size in ("Large", "Medium", "Small"):
            url = primary.get(size, {}).get("URL")
            if url:
                image_url = url
                break

        # --- Price ---
        price: float | None = None
        currency: str | None = None
        listings = item.get("Offers", {}).get("Listings", [])
        if listings:
            price_info = listings[0].get("Price", {})
            price = price_info.get("Amount")
            currency = price_info.get("Currency")
            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None

        # --- Customer reviews ---
        rating: float | None = None
        ratings_count: int | None = None
        reviews = item.get("CustomerReviews", {})
        star_rating = reviews.get("StarRating", {}).get("Value")
        count = reviews.get("Count")
        if star_rating is not None:
            try:
                rating = float(star_rating)
            except (TypeError, ValueError):
                pass
        if count is not None:
            try:
                ratings_count = int(count)
            except (TypeError, ValueError):
                pass

        return {
            "asin": asin,
            "title": title,
            "brand": brand,
            "image_url": image_url,
            "price": price,
            "currency": currency,
            "rating": rating,
            "ratings_count": ratings_count,
            "detail_page_url": detail_page_url,
        }


# ---------------------------------------------------------------------------
# Convenience factory — reads credentials from environment variables
# ---------------------------------------------------------------------------

def create_amazon_api(region: str = "us-east-1") -> AmazonCreatorsAPI:
    """Return an :class:`AmazonCreatorsAPI` instance configured from env vars.

    Environment variables read:
        ``AMAZON_ACCESS_KEY``, ``AMAZON_SECRET_KEY``, ``AMAZON_ASSOCIATE_TAG``
    """
    return AmazonCreatorsAPI(
        access_key=os.getenv("AMAZON_ACCESS_KEY"),
        secret_key=os.getenv("AMAZON_SECRET_KEY"),
        associate_tag=os.getenv("AMAZON_ASSOCIATE_TAG"),
        region=region,
    )

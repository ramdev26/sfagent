# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""``StorefrontBackend`` over Supplement Factory WooCommerce.

Catalog/cart use the public Store API. Order status uses the authenticated REST API
(``WOOCOMMERCE_CONSUMER_KEY`` / ``WOOCOMMERCE_CONSUMER_SECRET``). Checkout always
hands off to the live store; this backend never places an order or charges a card.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime
from typing import Any

import httpx

from demo_common.storefront_fixtures import SessionCarts, preferences_of, search_help
from shopping_agent import (
    Cart,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    OrderItem,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_WC_STATUS: dict[str, OrderStatus] = {
    "pending": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "on-hold": OrderStatus.PROCESSING,
    "completed": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "refunded": OrderStatus.REFUNDED,
    "failed": OrderStatus.CANCELLED,
    "checkout-draft": OrderStatus.PROCESSING,
}


def _plain(text: str | None, *, limit: int = 1200) -> str | None:
    if not text:
        return None
    cleaned = _WS.sub(" ", _TAG.sub(" ", html.unescape(text))).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _money(prices: dict[str, Any] | None) -> tuple[float, str]:
    if not prices:
        return 0.0, "LKR"
    minor = int(prices.get("currency_minor_unit") or 2)
    raw = prices.get("price") or prices.get("regular_price") or "0"
    try:
        amount = int(str(raw)) / (10**minor)
    except ValueError:
        amount = 0.0
    return round(amount, 2), str(prices.get("currency_code") or "LKR")


def _image_url(payload: dict[str, Any]) -> str | None:
    images = payload.get("images") or []
    if not images:
        return None
    first = images[0]
    if isinstance(first, dict):
        return first.get("src") or first.get("thumbnail")
    return None


def _category(payload: dict[str, Any]) -> str | None:
    cats = payload.get("categories") or []
    if not cats:
        return None
    name = cats[0].get("name") if isinstance(cats[0], dict) else None
    return _plain(name, limit=80)


def _brand(payload: dict[str, Any]) -> str | None:
    for key in ("brands",):
        brands = payload.get(key) or []
        if brands and isinstance(brands[0], dict) and brands[0].get("name"):
            return _plain(brands[0]["name"], limit=80)
    for attr in payload.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        if str(attr.get("name") or "").lower() in {"brand", "brands"}:
            terms = attr.get("terms") or []
            if terms and isinstance(terms[0], dict):
                return _plain(terms[0].get("name"), limit=80)
    return None


def _stock(payload: dict[str, Any]) -> bool:
    if "is_in_stock" in payload:
        return bool(payload["is_in_stock"])
    status = str(payload.get("stock_status") or "").lower()
    if status:
        return status == "instock"
    return True


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.now()


class WooCommerceStorefront(StorefrontBackend):
    """Live WooCommerce catalog for Supplement Factory; local session carts for the demo UI."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        store_name: str = "Supplement Factory",
        users: dict[str, UserPreferences] | None = None,
        policies: list[Policy] | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        demo_customer_id: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.store_name = store_name
        self.base_url = (base_url or os.environ.get("WOOCOMMERCE_URL") or "https://supplementfactory.lk").rstrip(
            "/"
        )
        self.store_api = f"{self.base_url}/wp-json/wc/store/v1"
        self.rest_api = f"{self.base_url}/wp-json/wc/v3"
        self.products: dict[str, ProductDetails] = {}
        self._users = users or {
            "demo-user": UserPreferences(
                user_id="demo-user",
                display_name="Guest",
                loyalty_tier="Guest",
                default_location="Colombo",
                preferences={
                    "interest": "performance nutrition and recovery",
                    "shipping": "island-wide Sri Lanka delivery",
                },
            )
        }
        self._policies = policies or []
        self._carts = SessionCarts()
        self._consumer_key = consumer_key or os.environ.get("WOOCOMMERCE_CONSUMER_KEY") or ""
        self._consumer_secret = consumer_secret or os.environ.get("WOOCOMMERCE_CONSUMER_SECRET") or ""
        customer_raw = demo_customer_id or os.environ.get("WOOCOMMERCE_DEMO_CUSTOMER_ID") or ""
        self._demo_customer_id = int(customer_raw) if str(customer_raw).isdigit() else None
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "commerce-agents-woocommerce/0.1"},
        )
        self._rest: httpx.AsyncClient | None = None
        if self._consumer_key and self._consumer_secret:
            self._rest = httpx.AsyncClient(
                timeout=timeout,
                auth=(self._consumer_key, self._consumer_secret),
                headers={"Accept": "application/json", "User-Agent": "commerce-agents-woocommerce/0.1"},
            )

    @property
    def has_rest(self) -> bool:
        return self._rest is not None

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._rest is not None:
            await self._rest.aclose()

    def warm_catalog_sync(self, limit: int = 24) -> None:
        """Prefetch popular products so the storefront home grid has tiles at boot."""
        try:
            with httpx.Client(
                timeout=self._client.timeout,
                headers=dict(self._client.headers),
            ) as client:
                response = client.get(
                    f"{self.store_api}/products",
                    params={"per_page": limit, "orderby": "popularity", "page": 1},
                )
                response.raise_for_status()
                rows = response.json()
        except Exception:
            logger.exception("Failed to warm WooCommerce catalog from %s", self.base_url)
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, dict):
                product = self._map_product(row, details=False)
                self.products[product.product_id] = ProductDetails(**product.model_dump())

    async def warm_catalog(self, limit: int = 24) -> None:
        try:
            rows = await self._get_json(
                "/products",
                params={"per_page": limit, "orderby": "popularity", "page": 1},
            )
        except Exception:
            logger.exception("Failed to warm WooCommerce catalog from %s", self.base_url)
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if isinstance(row, dict):
                product = self._map_product(row, details=False)
                self.products[product.product_id] = ProductDetails(**product.model_dump())

    def product(self, product_id: str) -> ProductDetails | None:
        return self.products.get(product_id)

    def reset_session(self, session_id: str) -> None:
        self._carts.reset(session_id)

    def recent_orders(self, limit: int = 6) -> list[Order]:
        return []

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(f"{self.store_api}{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def _rest_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any | None:
        if self._rest is None:
            return None
        response = await self._rest.get(f"{self.rest_api}{path}", params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _map_order(self, payload: dict[str, Any]) -> Order:
        status = _WC_STATUS.get(str(payload.get("status") or "").lower(), OrderStatus.PROCESSING)
        items: list[OrderItem] = []
        for line in payload.get("line_items") or []:
            if not isinstance(line, dict):
                continue
            pid = line.get("variation_id") or line.get("product_id") or 0
            items.append(
                OrderItem(
                    product_id=str(pid),
                    title=_plain(line.get("name"), limit=200) or f"Item {pid}",
                    quantity=int(line.get("quantity") or 1),
                    price=float(line.get("price") or 0),
                )
            )
        try:
            total = float(payload.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        tracking = None
        meta = payload.get("meta_data") or []
        if isinstance(meta, list):
            for entry in meta:
                if isinstance(entry, dict) and str(entry.get("key") or "").lower() in {
                    "tracking_url",
                    "_tracking_url",
                }:
                    tracking = str(entry.get("value") or "") or None
                    break
        return Order(
            order_id=str(payload["id"]),
            status=status,
            placed_at=_parse_dt(payload.get("date_created_gmt") or payload.get("date_created")),
            items=items,
            total=total,
            currency=str(payload.get("currency") or "LKR"),
            estimated_delivery=None,
            tracking_url=tracking,
        )

    def _map_product(self, payload: dict[str, Any], *, details: bool) -> Product | ProductDetails:
        product_id = str(payload["id"])
        price, currency = _money(payload.get("prices") or {})
        options: dict[str, list[str]] = {}
        option_values: dict[str, str] = {}
        variant_of: str | None = None
        attributes: dict[str, str] = {}

        parent = payload.get("parent")
        ptype = str(payload.get("type") or "simple")
        if ptype == "variation" and parent:
            variant_of = str(parent)
            variation = _plain(payload.get("variation"), limit=200) or ""
            for part in variation.split(","):
                if ":" in part:
                    key, _, value = part.partition(":")
                    option_values[key.strip()] = html.unescape(value.strip())
        elif ptype == "variable":
            for attr in payload.get("attributes") or []:
                if not isinstance(attr, dict) or not attr.get("has_variations"):
                    continue
                name = str(attr.get("name") or "Option")
                terms = [
                    html.unescape(str(term.get("name")))
                    for term in (attr.get("terms") or [])
                    if isinstance(term, dict) and term.get("name")
                ]
                if terms:
                    options[name] = terms

        for attr in payload.get("attributes") or []:
            if not isinstance(attr, dict) or attr.get("has_variations"):
                continue
            name = str(attr.get("name") or "").strip()
            terms = attr.get("terms") or []
            if name and terms and isinstance(terms[0], dict) and terms[0].get("name"):
                attributes[name.lower().replace(" ", "_")] = html.unescape(str(terms[0]["name"]))

        sku = payload.get("sku")
        if sku:
            attributes["sku"] = str(sku)

        base = {
            "product_id": product_id,
            "title": _plain(payload.get("name"), limit=200) or f"Product {product_id}",
            "brand": _brand(payload),
            "price": price,
            "currency": currency,
            "rating": float(payload["average_rating"])
            if payload.get("average_rating") not in (None, "", "0", 0, 0.0)
            else None,
            "review_count": int(payload.get("review_count") or 0) or None,
            "image_url": _image_url(payload),
            "category": _category(payload),
            "attributes": attributes,
            "in_stock": _stock(payload),
            "short_description": _plain(payload.get("short_description"), limit=400),
            "options": options,
            "option_values": option_values,
            "variant_of": variant_of,
        }
        if not details:
            return Product(**base)
        return ProductDetails(
            **base,
            long_description=_plain(payload.get("description"), limit=2500),
            specs={k: v for k, v in attributes.items() if k != "sku"},
            variants=[],
        )

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        del session
        filters = filters or SearchFilters()
        params: dict[str, Any] = {
            "search": query,
            "per_page": max(1, min(limit, 24)),
            "page": 1,
        }
        if filters.min_price is not None:
            params["min_price"] = str(int(filters.min_price * 100))
        if filters.max_price is not None:
            params["max_price"] = str(int(filters.max_price * 100))
        if filters.category:
            params["category"] = filters.category
        if filters.sort == "price_asc":
            params["orderby"] = "price"
            params["order"] = "asc"
        elif filters.sort == "price_desc":
            params["orderby"] = "price"
            params["order"] = "desc"
        elif filters.sort == "rating":
            params["orderby"] = "rating"

        try:
            rows = await self._get_json("/products", params=params)
        except Exception:
            logger.exception("WooCommerce search failed for %r", query)
            return []
        if not isinstance(rows, list):
            return []

        results: list[Product] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            product = self._map_product(row, details=False)
            if filters.min_rating is not None and (product.rating or 0) < filters.min_rating:
                continue
            self.products[product.product_id] = ProductDetails(**product.model_dump())
            results.append(product)
            if len(results) >= limit:
                break
        return results

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        del session
        try:
            payload = await self._get_json(f"/products/{product_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.exception("WooCommerce product %s failed", product_id)
            return None
        except Exception:
            logger.exception("WooCommerce product %s failed", product_id)
            return None
        if not isinstance(payload, dict):
            return None

        details = self._map_product(payload, details=True)
        assert isinstance(details, ProductDetails)
        if details.has_options:
            try:
                variations = await self._get_json(
                    "/products",
                    params={
                        "type": "variation",
                        "parent": int(product_id),
                        "per_page": 50,
                    },
                )
            except Exception:
                logger.exception("WooCommerce variations for %s failed", product_id)
                variations = []
            if isinstance(variations, list):
                mapped: list[Product] = []
                for row in variations:
                    if not isinstance(row, dict):
                        continue
                    variant = self._map_product(row, details=False)
                    self.products[variant.product_id] = ProductDetails(**variant.model_dump())
                    mapped.append(variant)
                if mapped:
                    in_stock_prices = [v.price for v in mapped if v.in_stock]
                    details = details.model_copy(
                        update={
                            "variants": mapped,
                            "price": min(in_stock_prices) if in_stock_prices else mapped[0].price,
                            "in_stock": any(v.in_stock for v in mapped),
                            "currency": mapped[0].currency,
                        }
                    )
        self.products[details.product_id] = details
        return details

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        cart = self._carts.cart(session.session_id)
        return cart.model_copy(update={"currency": "LKR"})

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        product = await self.get_product_details(session, product_id)
        if product is None:
            raise Unavailable(f"product {product_id} is not available")
        if product.has_options:
            raise Unavailable(
                f"product {product_id} is a family; add one of its variants instead"
            )
        if not product.in_stock:
            raise Unavailable(f"product {product_id} is out of stock")
        lines = self._carts.lines(session.session_id)
        existing = lines.get(product_id)
        next_qty = quantity + (existing.quantity if existing else 0)
        cart = self._carts.put(session.session_id, product, next_qty)
        return cart.model_copy(update={"currency": "LKR"})

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        cart = self._carts.set_quantity(session.session_id, product_id, quantity)
        return cart.model_copy(update={"currency": "LKR"})

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        cart = self._carts.remove(session.session_id, product_id)
        return cart.model_copy(update={"currency": "LKR"})

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        return preferences_of(self._users, session.user_id)

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        del session
        if not cart.items:
            return []
        if len(cart.items) == 1:
            item = cart.items[0]
            return [
                CheckoutHandoff(
                    url=(
                        f"{self.base_url}/?add-to-cart={item.product_id}"
                        f"&quantity={item.quantity}"
                    ),
                    label="Buy on Supplement Factory",
                )
            ]
        return [
            CheckoutHandoff(
                url=f"{self.base_url}/cart/",
                label="Complete checkout on Supplement Factory",
            )
        ]

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        del session
        if self._rest is None or self._demo_customer_id is None:
            # Guest demo: no customer binding — ask for an order number instead.
            return []
        try:
            rows = await self._rest_get(
                "/orders",
                params={
                    "customer": self._demo_customer_id,
                    "per_page": max(1, min(limit, 20)),
                    "orderby": "date",
                    "order": "desc",
                },
            )
        except Exception:
            logger.exception("WooCommerce list orders failed")
            return []
        if not isinstance(rows, list):
            return []
        return [self._map_order(row) for row in rows if isinstance(row, dict)]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        del session
        if self._rest is None:
            return None
        digits = re.sub(r"\D", "", order_id)
        if not digits:
            return None
        try:
            payload = await self._rest_get(f"/orders/{digits}")
        except Exception:
            logger.exception("WooCommerce get order %s failed", order_id)
            return None
        if not isinstance(payload, dict):
            return None
        order = self._map_order(payload)
        for item in order.items:
            if item.product_id and item.product_id not in self.products:
                self.products[item.product_id] = ProductDetails(
                    product_id=item.product_id,
                    title=item.title,
                    price=item.price,
                    currency=order.currency,
                )
        return order

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        del session
        return search_help(self._policies, query)

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        del session, product_ids
        return [
            FulfillmentOption(
                method="delivery",
                eta="Next-day island-wide when ordered before 12PM (Colombo time)",
                fee=0.0,
            ),
            FulfillmentOption(
                method="pickup",
                eta="Ready at Supplement Factory outlets (09 locations)",
                fee=0.0,
            ),
        ]

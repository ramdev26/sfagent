# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""MerchantBackend over Supplement Factory WooCommerce REST API.

Reads catalog, orders, and stock from wc/v3. Writes are staged until an operator
approves; ``apply_change`` then PATCHes the live product (price, stock, content).
Promotions and campaigns are refused — WooCommerce has no first-class match here.
"""

from __future__ import annotations

import calendar
import html
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import httpx

from merchant_agent import (
    ActorKind,
    AlertCounts,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeLedger,
    ChangeNotApplicable,
    DataLimitation,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantAgentConfig,
    MerchantBackend,
    MerchantSessionContext,
    MetricPoint,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
)
from shopping_agent import Order, OrderItem, OrderStatus

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_LOW_STOCK = 5

# Full product payloads 500 on this store (plugin/theme bloat). Ask only for fields
# ``_map_listing`` needs — that returns the full catalog (154 SKUs).
_PRODUCT_FIELDS = (
    "id,name,type,status,sku,price,regular_price,stock_quantity,stock_status,"
    "manage_stock,categories,images,short_description,description,attributes,"
    "parent_id,variations"
)

_WC_ORDER_STATUS: dict[str, OrderStatus] = {
    "pending": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "on-hold": OrderStatus.PROCESSING,
    "completed": OrderStatus.DELIVERED,
    "cancelled": OrderStatus.CANCELLED,
    "refunded": OrderStatus.REFUNDED,
    "failed": OrderStatus.CANCELLED,
}


def _plain(text: str | None, *, limit: int = 800) -> str | None:
    if not text:
        return None
    cleaned = _WS.sub(" ", _TAG.sub(" ", html.unescape(text))).strip()
    return cleaned[:limit] if cleaned else None


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.now()


def _wc_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").lower()
    stock = int(payload.get("stock_quantity") or 0)
    manage = bool(payload.get("manage_stock"))
    if status == "draft":
        return "draft"
    if status == "private":
        return "paused"
    if manage and stock <= 0:
        return "out_of_stock"
    if status == "publish":
        return "active"
    return "paused"


class WooCommerceMerchant(MerchantBackend):
    def __init__(
        self,
        *,
        store_name: str = "Supplement Factory",
        base_url: str | None = None,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        config: MerchantAgentConfig | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.store_name = store_name
        self.base_url = (base_url or os.environ.get("WOOCOMMERCE_URL") or "https://supplementfactory.lk").rstrip(
            "/"
        )
        self.rest_api = f"{self.base_url}/wp-json/wc/v3"
        key = consumer_key or os.environ.get("WOOCOMMERCE_CONSUMER_KEY") or ""
        secret = consumer_secret or os.environ.get("WOOCOMMERCE_CONSUMER_SECRET") or ""
        if not key or not secret:
            raise RuntimeError("WOOCOMMERCE_CONSUMER_KEY and WOOCOMMERCE_CONSUMER_SECRET are required")
        self.config = config or MerchantAgentConfig(brand_name=store_name)
        self.ledger = ChangeLedger(self.config)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            auth=(key, secret),
            headers={"Accept": "application/json", "User-Agent": "commerce-agents-woocommerce-merchant/0.1"},
        )
        self._listings: dict[str, ListingDetails] = {}
        self._orders_cache: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        await self._client.aclose()

    async def warm(self) -> None:
        # Orders first so a /products outage can still seed the catalog from line items.
        try:
            await self._refresh_orders()
        except Exception:
            logger.exception("WooCommerce order warm failed; continuing with empty order cache")
        try:
            await self._refresh_listings()
        except Exception:
            logger.exception("WooCommerce listing warm failed; continuing with empty catalog cache")

    def warm_sync(self) -> None:
        import anyio

        anyio.run(self.warm)

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(f"{self.rest_api}{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def _patch(self, path: str, body: dict[str, Any]) -> Any:
        response = await self._client.patch(f"{self.rest_api}{path}", json=body)
        response.raise_for_status()
        return response.json()

    async def _refresh_listings(self, pages: int = 20) -> None:
        rows: list[dict[str, Any]] = []
        per_page = 100
        try:
            for page in range(1, pages + 1):
                batch = await self._get(
                    "/products",
                    params={
                        "per_page": per_page,
                        "page": page,
                        "status": "any",
                        "_fields": _PRODUCT_FIELDS,
                    },
                )
                if not isinstance(batch, list) or not batch:
                    break
                rows.extend(row for row in batch if isinstance(row, dict))
                if len(batch) < per_page:
                    break
        except Exception:
            logger.exception(
                "WooCommerce /products list failed; building catalog from orders and top sellers"
            )
            await self._refresh_listings_from_orders_and_sellers()
            return
        if not rows:
            logger.warning("WooCommerce returned zero products; using order/top-seller fallback")
            await self._refresh_listings_from_orders_and_sellers()
            return
        listings: dict[str, ListingDetails] = {}
        for row in rows:
            listing = self._map_listing(row, details=True)
            assert isinstance(listing, ListingDetails)
            listings[listing.listing_id] = listing
        self._listings = listings
        logger.info("Catalog loaded %s listings from WooCommerce /products", len(listings))

    async def _refresh_listings_from_orders_and_sellers(self) -> None:
        """Degraded catalog when /products is down (common during WC critical errors)."""
        listings: dict[str, ListingDetails] = {}

        def upsert(
            listing_id: str,
            *,
            title: str,
            price: float = 0.0,
            stock: int = 999,
            sales_qty: int | None = None,
        ) -> None:
            if not listing_id or listing_id == "0":
                return
            existing = listings.get(listing_id)
            if existing is not None:
                updates: dict[str, Any] = {}
                if price and (not existing.price or price > existing.price):
                    updates["price"] = price
                if title and len(title) > len(existing.title or ""):
                    updates["title"] = title
                if sales_qty is not None:
                    updates["sales_last_30d"] = sales_qty
                if updates:
                    listings[listing_id] = existing.model_copy(update=updates)
                return
            listings[listing_id] = ListingDetails(
                listing_id=listing_id,
                title=title or f"Product {listing_id}",
                status="active",
                price=price,
                currency="LKR",
                stock=stock,
                content_quality="needs_work",
                short_description=(
                    "Reconstructed from recent orders — WooCommerce /products is unavailable."
                ),
                sales_last_30d=sales_qty,
            )

        try:
            sellers = await self._get("/reports/top_sellers", params={"period": "month"})
        except Exception:
            logger.exception("top_sellers fallback failed")
            sellers = []
        if isinstance(sellers, list):
            for row in sellers:
                if not isinstance(row, dict):
                    continue
                upsert(
                    str(row.get("product_id") or ""),
                    title=_plain(row.get("name"), limit=200) or f"Product {row.get('product_id')}",
                    sales_qty=int(row.get("quantity") or 0),
                )

        if not self._orders_cache:
            try:
                await self._refresh_orders()
            except Exception:
                logger.exception("order refresh for catalog fallback failed")
        for order in self._orders_cache:
            for line in order.get("line_items") or []:
                if not isinstance(line, dict):
                    continue
                pid = line.get("product_id") or line.get("variation_id") or 0
                try:
                    price = float(line.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                upsert(
                    str(pid),
                    title=_plain(line.get("name"), limit=200) or f"Product {pid}",
                    price=price,
                )

        self._listings = listings
        logger.info("Catalog fallback loaded %s listings from orders/top sellers", len(listings))

    async def _refresh_orders(self, days: int = 30, pages: int = 10) -> None:
        after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        rows: list[dict[str, Any]] = []
        # Pull paid-like statuses first so a page cap is not wasted on cancelled rows.
        for page in range(1, pages + 1):
            batch = await self._get(
                "/orders",
                params={
                    "per_page": 50,
                    "page": page,
                    "after": after,
                    "orderby": "date",
                    "order": "desc",
                    "status": "processing,completed,on-hold,pending,failed,cancelled",
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < 50:
                break
        self._orders_cache = rows

    async def _sales_report(
        self,
        *,
        days: int | None = None,
        period: str | None = None,
        date_min: str | None = None,
        date_max: str | None = None,
    ) -> dict[str, Any] | None:
        """WooCommerce admin-aligned totals (not capped by order page fetches)."""
        attempts: list[dict[str, Any]] = []
        if date_min and date_max:
            attempts.append({"date_min": date_min, "date_max": date_max})
        if period:
            attempts.append({"period": period})
        if days is not None:
            end = datetime.now().date()
            start = end - timedelta(days=days - 1)
            attempts.append({"date_min": start.isoformat(), "date_max": end.isoformat()})
        if not attempts:
            attempts.append({"period": "month"})
        for params in attempts:
            try:
                report = await self._get("/reports/sales", params=params)
            except Exception:
                logger.exception("WooCommerce sales report failed params=%s", params)
                continue
            if isinstance(report, list) and report and isinstance(report[0], dict):
                return report[0]
            if isinstance(report, dict):
                return report
        return None

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            return float(value if value is not None else default)
        except (TypeError, ValueError):
            return default

    def _summarize_sales_report(self, report: dict[str, Any] | None, *, label: str) -> dict[str, Any]:
        if not report:
            return {
                "label": label,
                "sales": 0.0,
                "net_sales": 0.0,
                "average_daily_sales": 0.0,
                "orders": 0,
                "items": 0,
                "shipping": 0.0,
                "refunds": 0.0,
                "discount": 0.0,
                "tax": 0.0,
                "customers": 0,
                "average_order_value": None,
                "daily": [],
            }
        sales = self._num(report.get("total_sales") or report.get("net_sales"))
        net = self._num(report.get("net_sales") or report.get("total_sales"))
        orders = int(self._num(report.get("total_orders")))
        items = int(self._num(report.get("total_items")))
        daily: list[dict[str, Any]] = []
        customers = 0
        totals = report.get("totals")
        if isinstance(totals, dict):
            for day, row in sorted(totals.items()):
                if not isinstance(row, dict):
                    continue
                day_customers = int(self._num(row.get("customers")))
                customers += day_customers
                daily.append(
                    {
                        "date": day,
                        "sales": round(self._num(row.get("sales")), 2),
                        "orders": int(self._num(row.get("orders"))),
                        "items": int(self._num(row.get("items"))),
                        "shipping": round(self._num(row.get("shipping")), 2),
                        "customers": day_customers,
                    }
                )
        return {
            "label": label,
            "sales": round(sales, 2),
            "net_sales": round(net, 2),
            "average_daily_sales": round(self._num(report.get("average_sales")), 2),
            "orders": orders,
            "items": items,
            "shipping": round(self._num(report.get("total_shipping")), 2),
            "refunds": round(self._num(report.get("total_refunds")), 2),
            "discount": round(self._num(report.get("total_discount")), 2),
            "tax": round(self._num(report.get("total_tax")), 2),
            "customers": customers,
            # AOV on net sales matches WooCommerce Analytics.
            "average_order_value": round(net / orders, 2) if orders else None,
            "daily": daily,
        }

    async def _top_sellers(self, *, period: str = "month", limit: int = 25) -> list[dict[str, Any]]:
        try:
            rows = await self._get("/reports/top_sellers", params={"period": period})
        except Exception:
            logger.exception("top_sellers failed period=%s", period)
            return []
        out: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return out
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            listing_id = str(row.get("product_id") or "")
            listing = self._listings.get(listing_id)
            qty = int(self._num(row.get("quantity")))
            price = listing.price if listing else 0.0
            out.append(
                {
                    "listing_id": listing_id,
                    "title": _plain(row.get("name"), limit=200) or (listing.title if listing else listing_id),
                    "quantity": qty,
                    "estimated_revenue": round(price * qty, 2) if price else None,
                    "category": listing.category if listing else None,
                    "image_url": listing.image_url if listing else None,
                    "price": price or None,
                }
            )
        return out

    def _order_mix_from_cache(self) -> dict[str, Any]:
        """Payment methods and category mix from the warm order cache (recent window)."""
        paid_statuses = {"processing", "completed", "on-hold"}
        by_payment: dict[str, dict[str, float]] = defaultdict(lambda: {"orders": 0, "sales": 0.0})
        by_status: dict[str, int] = defaultdict(int)
        by_category: dict[str, dict[str, float]] = defaultdict(lambda: {"quantity": 0, "sales": 0.0})
        for order in self._orders_cache:
            status = str(order.get("status") or "unknown")
            by_status[status] += 1
            if status not in paid_statuses:
                continue
            total = self._num(order.get("total"))
            method = (
                _plain(order.get("payment_method_title"), limit=80)
                or _plain(order.get("payment_method"), limit=40)
                or "Unknown"
            )
            by_payment[method]["orders"] += 1
            by_payment[method]["sales"] += total
            for line in order.get("line_items") or []:
                if not isinstance(line, dict):
                    continue
                pid = str(line.get("product_id") or line.get("variation_id") or "")
                listing = self._listings.get(pid)
                category = (listing.category if listing else None) or "Uncategorized"
                qty = int(self._num(line.get("quantity"), 1))
                line_total = self._num(line.get("total") or line.get("subtotal"))
                if not line_total:
                    line_total = self._num(line.get("price")) * qty
                by_category[category]["quantity"] += qty
                by_category[category]["sales"] += line_total
        payments = [
            {
                "method": name,
                "orders": int(stats["orders"]),
                "sales": round(stats["sales"], 2),
            }
            for name, stats in sorted(by_payment.items(), key=lambda item: item[1]["sales"], reverse=True)
        ]
        categories = [
            {
                "category": name,
                "quantity": int(stats["quantity"]),
                "sales": round(stats["sales"], 2),
            }
            for name, stats in sorted(by_category.items(), key=lambda item: item[1]["sales"], reverse=True)[:12]
        ]
        statuses = [
            {"status": name, "orders": count}
            for name, count in sorted(by_status.items(), key=lambda item: item[1], reverse=True)
        ]
        return {"payments": payments, "categories": categories, "statuses_recent": statuses}

    @staticmethod
    def _pct_change(current: float, prior: float) -> float | None:
        if prior == 0:
            return None if current == 0 else 100.0
        return round(((current - prior) / prior) * 100, 1)

    @staticmethod
    def _month_to_date_range(*, months_ago: int = 0) -> tuple[datetime, datetime, str]:
        """Month-to-date through today.

        months_ago=0 → 1st of this month → today (e.g. Oct 1–15).
        months_ago=1 → 1st of last month → today (e.g. Sep 1–Oct 15).
        """
        today = datetime.now().date()
        year = today.year
        month = today.month - months_ago
        while month <= 0:
            month += 12
            year -= 1
        start = datetime(year, month, 1).date()
        end = today
        if start.month == end.month and start.year == end.year:
            span = f"{start.strftime('%b')} {start.day}–{end.day}"
        else:
            span = f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"
        if months_ago == 0:
            title = f"This month ({span})"
        elif months_ago == 1:
            title = f"Past month to date ({span})"
        else:
            title = f"Since {start.strftime('%b %Y')} ({span})"
        return start, end, title

    def _attach_changes(self, block: dict[str, Any], prior: dict[str, Any] | None) -> None:
        if not prior:
            block["sales_change_pct"] = None
            block["orders_change_pct"] = None
            block["items_change_pct"] = None
            block["aov_change_pct"] = None
            return
        block["sales_change_pct"] = self._pct_change(block["net_sales"], prior["net_sales"])
        block["orders_change_pct"] = self._pct_change(float(block["orders"]), float(prior["orders"]))
        block["items_change_pct"] = self._pct_change(float(block["items"]), float(prior["items"]))
        if block["average_order_value"] is not None and prior["average_order_value"] is not None:
            block["aov_change_pct"] = self._pct_change(
                block["average_order_value"], prior["average_order_value"]
            )
        else:
            block["aov_change_pct"] = None

    async def sales_analytics(self) -> dict[str, Any]:
        """Detailed sales analytics for the merchant portal Sales tab."""
        if not self._orders_cache:
            try:
                await self._refresh_orders()
            except Exception:
                logger.exception("order refresh for sales analytics failed")

        mtd_start, mtd_end, mtd_label = self._month_to_date_range(months_ago=0)
        prior_mtd_start, prior_mtd_end, prior_mtd_label = self._month_to_date_range(months_ago=1)

        month = self._summarize_sales_report(
            await self._sales_report(
                period="month",
                date_min=mtd_start.isoformat(),
                date_max=mtd_end.isoformat(),
            ),
            label=mtd_label,
        )
        prior_mtd = self._summarize_sales_report(
            await self._sales_report(
                date_min=prior_mtd_start.isoformat(),
                date_max=prior_mtd_end.isoformat(),
            ),
            label=prior_mtd_label,
        )
        last_month = self._summarize_sales_report(
            await self._sales_report(period="last_month", days=None),
            label="Last month (full)",
        )
        last_30 = self._summarize_sales_report(
            await self._sales_report(days=30, period=None),
            label="Last 30 days",
        )

        # This MTD vs past MTD; past MTD vs full last month; 30d vs full last month.
        self._attach_changes(month, prior_mtd)
        self._attach_changes(prior_mtd, last_month)
        self._attach_changes(last_30, last_month)
        self._attach_changes(last_month, None)

        month["date_start"] = mtd_start.isoformat()
        month["date_end"] = mtd_end.isoformat()
        prior_mtd["date_start"] = prior_mtd_start.isoformat()
        prior_mtd["date_end"] = prior_mtd_end.isoformat()

        top_month = await self._top_sellers(period="month")
        top_last = await self._top_sellers(period="last_month")
        mix = self._order_mix_from_cache()

        order_totals: list[dict[str, Any]] = []
        try:
            totals = await self._get("/reports/orders/totals")
            if isinstance(totals, list):
                for row in totals:
                    if isinstance(row, dict):
                        order_totals.append(
                            {
                                "status": str(row.get("slug") or row.get("name") or ""),
                                "label": _plain(row.get("name"), limit=60)
                                or str(row.get("slug") or ""),
                                "orders": int(self._num(row.get("total"))),
                            }
                        )
        except Exception:
            logger.exception("order status totals failed")

        return {
            "currency": "LKR",
            "default_period": "month",
            "periods": {
                "month": month,
                "prior_mtd": prior_mtd,
                "last_month": last_month,
                "last_30_days": last_30,
            },
            "top_sellers": {
                "month": top_month,
                "prior_mtd": top_last,
                "last_month": top_last,
                "last_30_days": top_month,
            },
            "mix": mix,
            "order_status_lifetime": order_totals,
            "note": (
                "Sales, orders, shipping, and daily totals come from WooCommerce reports. "
                "Past month to date runs from the 1st of last month through today "
                "(e.g. Sep 1–Oct 15). Payment and category mix use recent cached orders."
            ),
        }

    def _map_listing(self, payload: dict[str, Any], *, details: bool) -> Listing | ListingDetails:
        listing_id = str(payload["id"])
        try:
            price = float(payload.get("price") or payload.get("regular_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if payload.get("manage_stock"):
            stock = int(payload.get("stock_quantity") or 0)
        elif payload.get("stock_status") == "outofstock":
            stock = 0
        else:
            # Stock is not tracked in WooCommerce — do not invent a low number that
            # trips low-stock alerts. Treat as adequately stocked.
            stock = 999
        images = payload.get("images") or []
        image_url = images[0].get("src") if images and isinstance(images[0], dict) else None
        cats = payload.get("categories") or []
        category = cats[0].get("name") if cats and isinstance(cats[0], dict) else None
        options: dict[str, list[str]] = {}
        option_values: dict[str, str] = {}
        variant_of: str | None = None
        ptype = str(payload.get("type") or "simple")
        if ptype == "variation" and payload.get("parent_id"):
            variant_of = str(payload["parent_id"])
            for attr in payload.get("attributes") or []:
                if isinstance(attr, dict) and attr.get("name") and attr.get("option"):
                    option_values[str(attr["name"])] = html.unescape(str(attr["option"]))
        elif ptype == "variable":
            for attr in payload.get("attributes") or []:
                if not isinstance(attr, dict) or not attr.get("variation"):
                    continue
                name = str(attr.get("name") or "Option")
                options[name] = [html.unescape(str(v)) for v in (attr.get("options") or [])]

        base = {
            "listing_id": listing_id,
            "title": _plain(payload.get("name"), limit=200) or f"Product {listing_id}",
            "status": _wc_status(payload),  # type: ignore[arg-type]
            "price": price,
            "currency": "LKR",
            "stock": stock,
            "category": _plain(category, limit=80),
            "content_quality": "good" if _plain(payload.get("description"), limit=40) else "needs_work",
            "attributes": {"sku": str(payload["sku"])} if payload.get("sku") else {},
            "image_url": image_url,
            "short_description": _plain(payload.get("short_description"), limit=280),
            "options": options,
            "option_values": option_values,
            "variant_of": variant_of,
        }
        if not details:
            return Listing(**base)
        return ListingDetails(
            **base,
            long_description=_plain(payload.get("description"), limit=2000),
            variants=[],
        )

    def all_listings(self) -> list[Listing]:
        return [
            Listing(**listing.model_dump(exclude={"long_description", "review_snippets", "variants", "sales_last_30d", "return_rate_pct", "missing_attributes"}))
            for listing in self._listings.values()
            if listing.variant_of is None
        ]

    def recent_storefront_orders(self, limit: int = 6) -> list[Order]:
        out: list[Order] = []
        for row in self._orders_cache[:limit]:
            out.append(self._map_order(row))
        return out

    def _map_order(self, payload: dict[str, Any]) -> Order:
        status = _WC_ORDER_STATUS.get(str(payload.get("status") or "").lower(), OrderStatus.PROCESSING)
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
        return Order(
            order_id=str(payload["id"]),
            status=status,
            placed_at=_parse_dt(payload.get("date_created_gmt") or payload.get("date_created")),
            items=items,
            total=float(payload.get("total") or 0),
            currency=str(payload.get("currency") or "LKR"),
        )

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        """Home KPIs match WooCommerce Analytics month-to-date net sales."""
        if not self._orders_cache:
            await self._refresh_orders()

        end = datetime.now().date()
        start = end.replace(day=1)
        period_label = period or f"{start.isoformat()}/{end.isoformat()}"

        report = await self._sales_report(
            period="month",
            date_min=start.isoformat(),
            date_max=end.isoformat(),
        )
        sales_change_pct: float | None = None
        orders_change_pct: float | None = None
        compare_to: str | None = None
        if report is not None:
            # Net sales matches Woo Analytics "Net sales"; gross includes shipping.
            sales = self._num(report.get("net_sales") or report.get("total_sales"))
            orders = int(self._num(report.get("total_orders")))
            note = "Net sales, month to date — same basis as WooCommerce Analytics."
            # Same day-span last month (e.g. Oct 1–15 vs Sep 1–15), not year-over-year.
            if start.month == 1:
                prior_start = start.replace(year=start.year - 1, month=12, day=1)
            else:
                prior_start = start.replace(month=start.month - 1, day=1)
            prior_last_day = calendar.monthrange(prior_start.year, prior_start.month)[1]
            prior_end = prior_start.replace(day=min(end.day, prior_last_day))
            prior = await self._sales_report(
                date_min=prior_start.isoformat(),
                date_max=prior_end.isoformat(),
            )
            if prior is not None:
                prior_sales = self._num(prior.get("net_sales") or prior.get("total_sales"))
                prior_orders = int(self._num(prior.get("total_orders")))
                sales_change_pct = self._pct_change(sales, prior_sales)
                orders_change_pct = self._pct_change(float(orders), float(prior_orders))
                compare_to = f"{prior_start.isoformat()}/{prior_end.isoformat()}"
        else:
            paid = [
                o
                for o in self._orders_cache
                if str(o.get("status")) in {"processing", "completed", "on-hold"}
            ]
            sales = sum(self._num(o.get("total")) for o in paid)
            orders = len(paid)
            note = "Reports unavailable; summed from a capped recent-order fetch."
        aov = round(sales / orders, 2) if orders else None
        alerts_list = await self.get_inventory_alerts(session)
        issues = await self.get_order_issues(session)
        return BusinessSnapshot(
            period=period_label,
            compare_to=compare_to,
            sales=round(sales, 2),
            orders=orders,
            traffic=None,
            conversion_rate=None,
            average_order_value=aov,
            sales_change_pct=sales_change_pct,
            orders_change_pct=orders_change_pct,
            currency="LKR",
            alerts=AlertCounts(
                low_stock=sum(1 for a in alerts_list if a.kind == "low_stock"),
                slow_movers=sum(1 for a in alerts_list if a.kind == "slow_mover"),
                order_issues=len(issues),
                pending_changes=len(self.ledger.pending()),
            ),
            note=note,
        )

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        del session, period, granularity, segment
        if metric not in {"sales", "orders"}:
            return MetricSeries(
                metric=metric,
                unit=None,
                points=[],
                note=f"Metric '{metric}' is not available from WooCommerce alone.",
            )
        if not self._orders_cache:
            await self._refresh_orders()
        buckets: dict[str, float] = defaultdict(float)
        for order in self._orders_cache:
            if str(order.get("status")) not in {"processing", "completed", "on-hold"}:
                continue
            day = str(order.get("date_created") or "")[:10]
            if not day:
                continue
            if metric == "sales":
                buckets[day] += float(order.get("total") or 0)
            else:
                buckets[day] += 1
        points = [MetricPoint(date=day, value=round(value, 2)) for day, value in sorted(buckets.items())]
        return MetricSeries(
            metric=metric,
            unit="LKR" if metric == "sales" else "count",
            points=points,
            note="Daily totals from WooCommerce orders in the last ~30 days.",
        )

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        del session, campaign_id
        return []

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        del session
        filters = filters or ListingFilters()
        q = query.lower().strip()
        results: list[Listing] = []
        for listing in self.all_listings():
            hay = f"{listing.title} {listing.category or ''} {listing.short_description or ''}".lower()
            if q and q not in hay:
                continue
            if filters.status and listing.status != filters.status:
                continue
            if filters.category and (listing.category or "").lower() != filters.category.lower():
                continue
            if filters.max_stock is not None and listing.stock > filters.max_stock:
                continue
            results.append(listing)
            if len(results) >= limit:
                break
        if not results and q:
            # Live search when the warm cache misses.
            try:
                rows = await self._get(
                    "/products",
                    params={
                        "search": query,
                        "per_page": limit,
                        "status": "any",
                        "_fields": _PRODUCT_FIELDS,
                    },
                )
            except Exception:
                logger.exception("WooCommerce listing search failed")
                return []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        mapped = self._map_listing(row, details=True)
                        assert isinstance(mapped, ListingDetails)
                        self._listings[mapped.listing_id] = mapped
                        results.append(Listing(**mapped.model_dump(exclude={"long_description", "review_snippets", "variants", "sales_last_30d", "return_rate_pct", "missing_attributes"})))
        return results[:limit]

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        del session
        if listing_id in self._listings:
            return self._listings[listing_id]
        try:
            payload = await self._get(
                f"/products/{listing_id}",
                params={"_fields": _PRODUCT_FIELDS},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            return None
        details = self._map_listing(payload, details=True)
        assert isinstance(details, ListingDetails)
        if details.has_options:
            try:
                variations = await self._get(
                    f"/products/{listing_id}/variations",
                    params={
                        "per_page": 50,
                        "_fields": (
                            "id,name,status,sku,price,regular_price,stock_quantity,"
                            "stock_status,manage_stock,attributes,image,parent_id"
                        ),
                    },
                )
            except Exception:
                variations = []
            mapped: list[Listing] = []
            if isinstance(variations, list):
                for row in variations:
                    if not isinstance(row, dict):
                        continue
                    # Variation payloads lack parent_id sometimes; force it.
                    row = {**row, "parent_id": int(listing_id), "type": "variation"}
                    variant = self._map_listing(row, details=False)
                    self._listings[variant.listing_id] = ListingDetails(**variant.model_dump())
                    mapped.append(variant)
            if mapped:
                details = details.model_copy(
                    update={
                        "variants": mapped,
                        "price": min(v.price for v in mapped),
                        "stock": sum(v.stock for v in mapped),
                    }
                )
        self._listings[details.listing_id] = details
        return details

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        del session
        alerts: list[InventoryAlert] = []
        for listing in self._listings.values():
            if listing.has_options:
                continue
            if listing.stock <= _LOW_STOCK and listing.status in {"active", "out_of_stock"}:
                alerts.append(
                    InventoryAlert(
                        listing_id=listing.listing_id,
                        title=listing.title,
                        kind="low_stock",
                        option_values=listing.option_values,
                        variant_of=listing.variant_of,
                        stock=listing.stock,
                        threshold=_LOW_STOCK,
                        storefront_visible=listing.status == "active",
                    )
                )
        return alerts[:40]

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        del session
        if not self._orders_cache:
            await self._refresh_orders()
        issues: list[OrderIssue] = []
        for order in self._orders_cache:
            status = str(order.get("status") or "")
            oid = str(order["id"])
            if status in {"on-hold", "failed", "cancelled"}:
                issues.append(
                    OrderIssue(
                        issue_id=f"{oid}-{status}",
                        order_id=oid,
                        kind="delayed" if status == "on-hold" else "buyer_message",
                        summary=f"Order {oid} is {status}",
                        opened_at=_parse_dt(order.get("date_created")),
                    )
                )
        return issues[:40]

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        listing = await self.get_listing(session, listing_id)
        if listing is None:
            return None
        variants_ctx: list[PricingContext] = []
        if listing.variants:
            for variant in listing.variants:
                variants_ctx.append(
                    PricingContext(
                        listing_id=variant.listing_id,
                        current_price=variant.price,
                        currency=variant.currency,
                        max_price_delta_pct=self.config.max_price_delta_pct,
                        max_promotion_discount_pct=self.config.max_promotion_discount_pct,
                        option_values=variant.option_values,
                    )
                )
        return PricingContext(
            listing_id=listing.listing_id,
            current_price=listing.price,
            currency=listing.currency,
            max_price_delta_pct=self.config.max_price_delta_pct,
            max_promotion_discount_pct=self.config.max_promotion_discount_pct,
            option_values=listing.option_values,
            variants=variants_ctx,
            min_price_basis="policy",
        )

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        listing = await self.get_listing(session, listing_id)
        if listing is None:
            raise ValueError(f"no listing {listing_id}")
        allowed = {"title", "short_description", "long_description", "status"}
        items = []
        for name, value in fields.items():
            if name not in allowed:
                raise ChangeNotApplicable(f"field '{name}' is not managed for WooCommerce listings")
            before = getattr(listing, name, None)
            items.append(ChangeItem(target=listing_id, field=name, before=before, after=value))
        return self.ledger.stage(
            kind=ChangeKind.LISTING_UPDATE,
            summary=note or f"Update listing {listing_id}",
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        change_items = []
        for item in items:
            listing = await self.get_listing(session, item.listing_id)
            if listing is None:
                raise ValueError(f"no listing {item.listing_id}")
            if listing.has_options:
                raise ValueError(f"{listing.listing_id} is priced per variant")
            change_items.append(
                ChangeItem(
                    target=listing.listing_id,
                    field="price",
                    before=listing.price,
                    after=item.new_price,
                )
            )
        return self.ledger.stage(
            kind=ChangeKind.PRICE_UPDATE,
            summary=note or f"Price update for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency="LKR",
        )

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        change_items = []
        for item in items:
            listing = await self.get_listing(session, item.listing_id)
            if listing is None:
                raise ValueError(f"no listing {item.listing_id}")
            if listing.has_options and item.action == "restock":
                raise ValueError(f"{listing.listing_id} is stocked per variant")
            if item.action == "restock":
                qty = getattr(item, "quantity", None)
                if qty is None:
                    raise ValueError("restock requires quantity")
                change_items.append(
                    ChangeItem(
                        target=listing.listing_id,
                        field="stock",
                        before=listing.stock,
                        after=int(qty),
                    )
                )
            elif item.action == "pause":
                change_items.append(
                    ChangeItem(
                        target=listing.listing_id,
                        field="status",
                        before=listing.status,
                        after="paused",
                    )
                )
            else:
                change_items.append(
                    ChangeItem(
                        target=listing.listing_id,
                        field="status",
                        before=listing.status,
                        after="active",
                    )
                )
        return self.ledger.stage(
            kind=ChangeKind.INVENTORY_ACTION,
            summary=note or f"Inventory action for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        del session, promotion
        raise ChangeNotApplicable(
            "WooCommerce promotions/coupons are not wired in this merchant agent yet"
        )

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        del session, campaign
        raise ChangeNotApplicable(
            "Campaign staging is not available; use your ads platform separately"
        )

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        del session
        return self.ledger.pending()

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        change = self.ledger.get(change_id)
        if change is None:
            raise ValueError(f"no change {change_id}")
        # Group field updates per product id, then PATCH WooCommerce.
        by_target: dict[str, dict[str, Any]] = defaultdict(dict)
        for item in change.items:
            by_target[item.target][item.field] = item.after
        for listing_id, fields in by_target.items():
            body: dict[str, Any] = {}
            if "title" in fields:
                body["name"] = fields["title"]
            if "short_description" in fields:
                body["short_description"] = fields["short_description"]
            if "long_description" in fields:
                body["description"] = fields["long_description"]
            if "price" in fields:
                body["regular_price"] = str(fields["price"])
            if "stock" in fields:
                body["manage_stock"] = True
                body["stock_quantity"] = int(fields["stock"])
            if "status" in fields:
                status = fields["status"]
                body["status"] = {
                    "active": "publish",
                    "paused": "private",
                    "draft": "draft",
                    "out_of_stock": "publish",
                }.get(str(status), "publish")
            if body:
                await self._patch(f"/products/{listing_id}", body)
                # Refresh local cache for this id.
                await self.get_listing(session, listing_id)
        return self.ledger.apply(change_id, session.operator)

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        return self.ledger.discard(change_id, session.operator, actor_kind)

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        del session
        return {
            "store": self.store_name,
            "currency": "LKR",
            "platform": "WooCommerce",
            "catalog_size": len(self.all_listings()),
            "limitations": [
                DataLimitation(
                    source="analytics",
                    note="Traffic and conversion need analytics; sales are WooCommerce only.",
                ).model_dump(),
                DataLimitation(
                    source="campaigns",
                    note="Ad campaigns and coupons are not managed from this agent yet.",
                ).model_dump(),
            ],
        }

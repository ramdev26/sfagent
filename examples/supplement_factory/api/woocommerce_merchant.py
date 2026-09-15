# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""MerchantBackend over Supplement Factory WooCommerce REST API.

Reads catalog, orders, and stock from wc/v3. Writes are staged until an operator
approves; ``apply_change`` then PATCHes the live product (price, stock, content).
Promotions and campaigns are refused — WooCommerce has no first-class match here.
"""

from __future__ import annotations

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
        await self._refresh_listings()
        await self._refresh_orders()

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

    async def _refresh_listings(self, pages: int = 3) -> None:
        rows: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            batch = await self._get(
                "/products",
                params={"per_page": 50, "page": page, "status": "any", "orderby": "popularity"},
            )
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < 50:
                break
        listings: dict[str, ListingDetails] = {}
        for row in rows:
            listing = self._map_listing(row, details=True)
            assert isinstance(listing, ListingDetails)
            listings[listing.listing_id] = listing
        self._listings = listings

    async def _refresh_orders(self, days: int = 30) -> None:
        after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
        rows: list[dict[str, Any]] = []
        for page in range(1, 4):
            batch = await self._get(
                "/orders",
                params={
                    "per_page": 50,
                    "page": page,
                    "after": after,
                    "orderby": "date",
                    "order": "desc",
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < 50:
                break
        self._orders_cache = rows

    def _map_listing(self, payload: dict[str, Any], *, details: bool) -> Listing | ListingDetails:
        listing_id = str(payload["id"])
        try:
            price = float(payload.get("price") or payload.get("regular_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        stock = int(payload.get("stock_quantity") or 0) if payload.get("manage_stock") else (
            0 if payload.get("stock_status") == "outofstock" else 25
        )
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
        period = period or "last_30_days"
        if not self._orders_cache:
            await self._refresh_orders()
        paid = [
            o
            for o in self._orders_cache
            if str(o.get("status")) in {"processing", "completed", "on-hold"}
        ]
        sales = sum(float(o.get("total") or 0) for o in paid)
        orders = len(paid)
        aov = round(sales / orders, 2) if orders else None
        alerts_list = await self.get_inventory_alerts(session)
        issues = await self.get_order_issues(session)
        return BusinessSnapshot(
            period=period,
            sales=round(sales, 2),
            orders=orders,
            traffic=None,
            conversion_rate=None,
            average_order_value=aov,
            currency="LKR",
            alerts=AlertCounts(
                low_stock=sum(1 for a in alerts_list if a.kind == "low_stock"),
                slow_movers=sum(1 for a in alerts_list if a.kind == "slow_mover"),
                order_issues=len(issues),
                pending_changes=len(self.ledger.pending()),
            ),
            note="Traffic and conversion need analytics; sales are from WooCommerce orders.",
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
                rows = await self._get("/products", params={"search": query, "per_page": limit})
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
            payload = await self._get(f"/products/{listing_id}")
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
                    f"/products/{listing_id}/variations", params={"per_page": 50}
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

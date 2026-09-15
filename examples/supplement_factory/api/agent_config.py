# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from merchant_agent import MerchantAgentConfig
from shopping_agent import ShoppingAgentConfig


def build_shopping_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name="Supplement Factory",
        assistant_name="Supp Factory Assistant",
        brand_voice="knowledgeable, direct, and athlete-friendly — authentic supplements for Sri Lanka",
        enable_orders=True,
        domain_search_notes=(
            "This is a live WooCommerce catalog of sports nutrition and supplements "
            "(protein, creatine, pre-workout, vitamins) sold in LKR for Sri Lanka. "
            "Prefer brand + product type queries (e.g. 'Applied Nutrition creatine'). "
            "Variable products have flavors — ask the customer to pick a variant before adding to cart. "
            "For order status, ask for the WooCommerce order number and look it up; "
            "guest sessions do not list a full order history unless a demo customer id is configured."
        ),
    )


def build_merchant_config() -> MerchantAgentConfig:
    return MerchantAgentConfig(
        brand_name="Supplement Factory",
        assistant_name="Merchant Assistant",
        brand_voice="concise, operational, and precise — Sri Lanka supplement retail",
        require_host_approval=True,
        approval_surface="the Approve button on the change preview card",
        enable_campaigns=False,
        # Listing edits, inventory, and pricing write through WooCommerce after approval.
        enable_listing_edits=True,
        enable_inventory=True,
        enable_pricing=True,
    )

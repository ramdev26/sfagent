# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Supplement Factory merchant API over live WooCommerce.

    uvicorn supplement_factory.api.main:app --app-dir examples --reload --port 8004
    python scripts/run_demo.py supplement_factory --merchant
"""

from __future__ import annotations

from commerce_common.memory import InMemoryMemoryStore
from demo_common import (
    REPO_ROOT,
    MerchantIdentity,
    MemorySeeder,
    build_merchant_router,
    build_storefront_host,
    load_demo_env,
)
from demo_common.storefront_fixtures import example_data_dir, load_policies, load_users
from merchant_agent_runtime import MerchantAgent
from shopping_agent import Order
from shopping_agent_runtime import ShoppingAgent

from .agent_config import build_merchant_config, build_shopping_config
from .woocommerce_backend import WooCommerceStorefront
from .woocommerce_merchant import WooCommerceMerchant

DATA_DIR = example_data_dir(__file__)
load_demo_env(DATA_DIR.parent)

IDENTITY = MerchantIdentity(merchant_id="supplement-factory", operator="Staff")

backend = WooCommerceStorefront(
    users=load_users(DATA_DIR),
    policies=load_policies(DATA_DIR),
)
backend.warm_catalog_sync()

merchant_config = build_merchant_config()
merchant = WooCommerceMerchant(config=merchant_config)
merchant.warm_sync()


def _recent_orders(limit: int = 6) -> list[Order]:
    return merchant.recent_storefront_orders(limit)


backend.recent_orders = _recent_orders  # type: ignore[method-assign]

shopping_agent = ShoppingAgent(
    backend=backend,
    skills_dir=REPO_ROOT / "shopping-agent" / "skills",
    config=build_shopping_config(),
    memory_store=InMemoryMemoryStore(),
)

merchant_agent = MerchantAgent(
    backend=merchant,
    skills_dir=REPO_ROOT / "merchant-agent" / "skills",
    config=merchant_config,
    memory_store=InMemoryMemoryStore(),
)

host = build_storefront_host(
    title="Supplement Factory API",
    example_root=DATA_DIR.parent,
    backend=backend,
    agent=shopping_agent,
    memory_seeder=MemorySeeder(
        DATA_DIR / "memory-seed.json", marker=DATA_DIR / ".memory-seeded.json"
    ),
)
app = host.app
app.include_router(
    build_merchant_router(
        storefront=backend,
        backend=merchant,
        agent=merchant_agent,
        identity=IDENTITY,
        example_dir="supplement_factory",
    ),
    prefix="/api/merchant",
)

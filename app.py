# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Vercel FastAPI entrypoint for the Supplement Factory merchant API.

Vercel looks for a FastAPI instance named ``app`` in this file. Local runs still
use ``uvicorn supplement_factory.api.main:app --app-dir examples``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_EXAMPLES = _ROOT / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from supplement_factory.api.main import app  # noqa: E402

__all__ = ["app"]

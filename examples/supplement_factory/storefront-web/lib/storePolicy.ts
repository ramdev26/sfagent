// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

/** Copied from ../data/policies.json; keep in sync with it. */
export const STORE_POLICY = {
  returnsShort: "Unopened returns",
  returnsLine:
    "Unopened supplements in original condition may be returned or exchanged per Supplement Factory policy. Contact the store before returning.",
  freeShippingThreshold: 0,
  standardShippingEta: "Next-day island-wide when ordered before 12PM",
} as const;

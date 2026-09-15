// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import path from "path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
  turbopack: {
    root: path.join(__dirname, "../.."),
  },
};

export default nextConfig;

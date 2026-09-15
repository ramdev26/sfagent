// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import path from "path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
  // Workspace lockfile lives in examples/; keep Turbopack rooted there.
  turbopack: {
    root: path.join(__dirname, "../.."),
  },
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "supplementfactory.lk",
        pathname: "/wp-content/**",
      },
    ],
  },
};

export default nextConfig;

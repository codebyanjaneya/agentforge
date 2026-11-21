import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin Turbopack's root to this project. Without it Next infers the root from the nearest lockfile it finds
  // walking upwards, which on a machine with a stray package-lock.json / node_modules in a parent directory
  // makes module resolution and file watching leave the project (observed during M1-G verification).
  turbopack: { root: path.resolve(process.cwd()) },
};

export default nextConfig;

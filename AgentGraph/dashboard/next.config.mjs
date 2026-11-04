import { readFileSync } from "node:fs";

// The console build stamp (data-console-build) is this package's version,
// read once at build time, so it cannot drift from package.json.
const { version } = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
);

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: "/dashboard",
  images: { unoptimized: true },
  reactStrictMode: true,
  env: { CONSOLE_BUILD: version },
};

export default nextConfig;

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["{apps,services,packages,tests}/**/*.test.ts"],
    environment: "node",
    passWithNoTests: false,
  },
});

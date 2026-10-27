import { defineConfig } from "vitest/config";
import path from "node:path";
export default defineConfig({ esbuild: { jsx: "automatic" }, test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], exclude: ["**/node_modules/**", "**/.next/**", "**/e2e/**", "**/.pwtmp/**", "**/test-results/**", "**/playwright-report/**"] }, resolve: { alias: { "@": path.resolve(__dirname, "src") } } });

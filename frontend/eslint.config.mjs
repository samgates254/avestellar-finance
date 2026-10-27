// ESLint 9 flat configuration for the Avestellar Finance frontend.
//
// Background: `eslint-config-next` boots by requiring
// `@rushstack/eslint-patch/modern-module-resolution`, and the only patch version
// available in this offline install (1.16.1) refuses to run against ESLint 9.39.1
// ("Failed to patch ESLint because the calling module was not recognized").
// Rather than disable linting, we reproduce the exact rule set that
// `next/core-web-vitals` + `next/typescript` apply, loading the underlying
// plugins (which are already installed) directly through FlatCompat. No rules
// are silenced and no blanket ignores are added beyond generated build and
// browser-test output directories.
import { FlatCompat } from "@eslint/eslintrc";
import path from "node:path";
import { fileURLToPath } from "node:url";
import tsParser from "@typescript-eslint/parser";

const compat = new FlatCompat({ baseDirectory: path.dirname(fileURLToPath(import.meta.url)) });

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "test-results/**", "playwright-report/**", ".pwtmp/**"] },

  // Base JavaScript/TypeScript parsing and shared settings, mirroring
  // eslint-config-next's parser options, env, and import/react settings.
  {
    files: ["**/*.{js,mjs,cjs,jsx,ts,tsx,mts,cts}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        window: "readonly",
        document: "readonly",
        navigator: "readonly",
        location: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
        fetch: "readonly",
        crypto: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        FormData: "readonly",
        File: "readonly",
        Blob: "readonly",
        process: "readonly",
        Buffer: "readonly",
        __dirname: "readonly",
        module: "readonly",
        require: "readonly",
      },
    },
    settings: {
      react: { version: "detect" },
      "import/resolver": {
        node: { extensions: [".js", ".jsx", ".ts", ".tsx"] },
        typescript: { alwaysTryTypes: true },
      },
    },
  },

  // The shared ESLint plugin configurations that eslint-config-next extends.
  ...compat.extends(
    "plugin:react/recommended",
    "plugin:react-hooks/recommended",
    "plugin:@next/next/recommended",
    "plugin:@next/next/core-web-vitals",
  ),

  // The precise rule overrides eslint-config-next applies on top of those
  // shared configs (import + jsx-a11y plugins are referenced by these rules).
  {
    files: ["**/*.{js,mjs,cjs,jsx,ts,tsx,mts,cts}"],
    plugins: {
      import: (await import("eslint-plugin-import")).default,
      "jsx-a11y": (await import("eslint-plugin-jsx-a11y")).default,
    },
    rules: {
      "import/no-anonymous-default-export": "warn",
      "react/no-unknown-property": "off",
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      "react/jsx-no-target-blank": "off",
      "jsx-a11y/alt-text": ["warn", { elements: ["img"], img: ["Image"] }],
      "jsx-a11y/aria-props": "warn",
      "jsx-a11y/aria-proptypes": "warn",
      "jsx-a11y/aria-unsupported-elements": "warn",
      "jsx-a11y/role-has-required-aria-props": "warn",
      "jsx-a11y/role-supports-aria-props": "warn",
    },
  },

  // next/typescript: @typescript-eslint/recommended with eslint-config-next's
  // two warning downgrades, applied with the TypeScript parser on TS files only.
  ...compat.extends("plugin:@typescript-eslint/recommended"),
  {
    files: ["**/*.{ts,tsx,mts,cts}"],
    languageOptions: { parser: tsParser, parserOptions: { sourceType: "module" } },
    rules: {
      "@typescript-eslint/no-unused-vars": "warn",
      "@typescript-eslint/no-unused-expressions": "warn",
    },
  },
];

export default config;

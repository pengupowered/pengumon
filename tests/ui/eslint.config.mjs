// ESLint flat config for the dashboard modules (src/pengumon/web/static) and the browser test (tests/ui/*.mjs, steps/). ESLint and `globals` are not project
// dependencies: they are pinned in tests/ui/runner/package-lock.json and come from the directory in $PGBROWSER (scripts/test-ui.sh). Run it with
//   node $PGBROWSER/node_modules/eslint/bin/eslint.js -c tests/ui/eslint.config.mjs src/pengumon/web/static/js
// `no-undef` is the point: module scripts are strict, so an implicit global or a forgotten import must fail here.
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.join(process.env.PGBROWSER, "package.json"));
const globals = require("globals");

export default [
  {
    // the browser test itself (runner, lib, step files): Node, with page.evaluate callbacks that run in the browser
    files: ["**/*.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.node, ...globals.browser, Chart: "readonly" },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["warn", { args: "none", caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-const-assign": "error",
    },
  },
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser, Chart: "readonly" },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["warn", { args: "none", caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-const-assign": "error",
      "no-import-assign": "error",
    },
  },
];

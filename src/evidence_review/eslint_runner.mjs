import fs from 'node:fs';
import { ESLint } from 'eslint';
import js from '@eslint/js';
import tsParser from '@typescript-eslint/parser';
import globals from 'globals';

// Fixed tool-owned configuration: do not execute the target project's config/plugins.
try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const eslint = new ESLint({
    cwd: input.root,
    overrideConfigFile: true,
    ignore: false,
    allowInlineConfig: false,
    overrideConfig: [
      {
        files: ['**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}'],
        languageOptions: {
          ecmaVersion: 'latest', sourceType: 'module',
          parserOptions: { ecmaFeatures: { jsx: true } },
          globals: { ...globals.browser, ...globals.node },
        },
        rules: { ...js.configs.recommended.rules, 'no-unused-vars': 'off',
          'no-eval': 'error', 'no-implied-eval': 'error', 'no-new-func': 'error' },
      },
      {
        files: ['**/*.{ts,tsx,mts,cts}'],
        languageOptions: { parser: tsParser },
        rules: { 'no-undef': 'off' },
      },
      { files: ['**/*.{cjs,cts}'], languageOptions: { sourceType: 'commonjs' } },
    ],
  });
  const results = [];
  for (const file of input.files) {
    const linted = await eslint.lintText(file.code, { filePath: file.path });
    results.push(...linted.map(({ filePath, messages }) => ({ filePath, messages })));
  }
  process.stdout.write(JSON.stringify({ version: ESLint.version, results }));
} catch (error) {
  process.stderr.write(String(error.message ?? error));
  process.exitCode = 2;
}

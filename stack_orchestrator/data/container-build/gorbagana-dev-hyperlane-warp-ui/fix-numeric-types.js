#!/usr/bin/env node
/**
 * fix-numeric-types.js — Post-sed fixup for YAML-compiled numeric fields.
 *
 * Problem: YAML sentinels (e.g. chainId: __GORCHAIN_CHAIN_ID__) compile to
 * JS strings ("__GORCHAIN_CHAIN_ID__"). After sed replaces the sentinel with
 * the actual value, it becomes "99999" (still a string). The Hyperlane SDK's
 * Zod schemas expect these to be numbers, not strings.
 *
 * Solution: After sed replacement, this script finds compiled JS chunks that
 * contain our chain config (identified by chain name values) and converts
 * stringified numbers back to real numbers for known numeric keys.
 *
 * Only processes files containing at least one of the chain names passed via
 * --chain-names, so SDK registry entries in other chunks are never touched.
 *
 * Usage: node fix-numeric-types.js --chain-names gorchain,solana
 */

const fs = require('fs');
const path = require('path');

// Keys that must be numbers in the Hyperlane SDK schemas
const NUMERIC_KEYS = [
  'chainId',
  'domainId',
  'decimals',
  'confirmations',
  'estimateBlockTime',
  'reorgPeriod',
];

function parseArgs() {
  const args = process.argv.slice(2);
  let chainNames = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--chain-names' && args[i + 1]) {
      chainNames = args[i + 1].split(',').map(s => s.trim()).filter(Boolean);
      i++;
    }
  }
  if (chainNames.length === 0) {
    console.error('Usage: node fix-numeric-types.js --chain-names name1,name2');
    process.exit(1);
  }
  return { chainNames };
}

function findJsFiles(dir) {
  const results = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...findJsFiles(full));
    } else if (entry.isFile() && /\.(js|json)$/.test(entry.name)) {
      results.push(full);
    }
  }
  return results;
}

function fixNumericTypes(content) {
  let modified = content;
  for (const key of NUMERIC_KEYS) {
    // Match key:"<digits>" or key:"<digits>.<digits>" (for estimateBlockTime)
    // in minified JS (no spaces) and pretty-printed JSON (with spaces/quotes)
    //
    // Patterns to handle:
    //   minified JS:  chainId:"99999"
    //   JSON:         "chainId": "99999"  or  "chainId":"99999"
    const re = new RegExp(
      `("${key}"\\s*:\\s*)"(\\d+\\.?\\d*)"`,
      'g'
    );
    modified = modified.replace(re, '$1$2');

    // Also handle unquoted key (minified JS object literal): chainId:"99999"
    const reUnquoted = new RegExp(
      `((?:^|[,{])\\s*${key}\\s*:\\s*)"(\\d+\\.?\\d*)"`,
      'g'
    );
    modified = modified.replace(reUnquoted, '$1$2');
  }
  return modified;
}

function main() {
  const { chainNames } = parseArgs();
  const nextDir = '/app/.next';

  if (!fs.existsSync(nextDir)) {
    console.error(`Directory ${nextDir} not found`);
    process.exit(1);
  }

  const files = findJsFiles(nextDir);
  let fixedCount = 0;

  for (const file of files) {
    const content = fs.readFileSync(file, 'utf8');

    // Only process files that contain at least one of our chain names.
    // This ensures we never touch SDK registry chunks that have their own
    // chainId fields (the bug that killed the previous sed-only approach).
    const hasOurConfig = chainNames.some(name => content.includes(name));
    if (!hasOurConfig) continue;

    const fixed = fixNumericTypes(content);
    if (fixed !== content) {
      fs.writeFileSync(file, fixed, 'utf8');
      fixedCount++;
      console.log(`  Fixed numeric types in: ${path.relative(nextDir, file)}`);
    }
  }

  console.log(`Fixed ${fixedCount} file(s)`);
}

main();

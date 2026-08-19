/**
 * Keeps the guide index in README.md equal to what the frontmatter says.
 *
 * The README lists all eighteen guides with their titles and descriptions. It
 * used to be typed by hand, which made it a mirror that goes stale silently: a
 * retargeting pass changed every title and almost every description, and the
 * README kept advertising the old ones. The README is a ranking surface, so a
 * stale one is a live SEO defect rather than an untidy file.
 *
 *     node scripts/readme-index.mjs           # check, exit 1 if stale
 *     node scripts/readme-index.mjs --write   # rewrite the generated regions
 *
 * The check runs inside `npm run verify`, so a frontmatter change that skips the
 * README fails the build rather than shipping. Everything outside the BEGIN/END
 * markers — the opening, the pillar introductions, contributing, and local
 * development — is left exactly as written.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { readGuides, REPO_ROOT } from './lib/pages.mjs';
import { checkCount, drift, renderReadme } from './lib/readme.mjs';

const README = path.join(REPO_ROOT, 'README.md');
const WRITE = process.argv.includes('--write');

const guides = readGuides();
const current = readFileSync(README, 'utf8');

let wanted;
try {
  wanted = renderReadme(current, guides);
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

const countProblem = checkCount(wanted, guides.length);
if (countProblem) {
  console.error(countProblem);
  process.exit(1);
}

if (wanted === current) {
  console.log(`readme check passed: ${guides.length} guides match the frontmatter`);
  process.exit(0);
}

if (WRITE) {
  writeFileSync(README, wanted);
  console.log(`readme index rewritten: ${guides.length} guides`);
  process.exit(0);
}

const lines = drift(wanted, current);
console.error(
  `README.md's generated guide index no longer matches src/content/guides/:\n${lines.slice(0, 12).join('\n')}` +
    (lines.length > 12 ? `\n  ...and ${lines.length - 12} more differing lines` : '') +
    '\n\nRun `npm run readme` and commit README.md.'
);
process.exit(1);

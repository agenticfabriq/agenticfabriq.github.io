/**
 * Fails when the committed Open Graph cards no longer describe the site.
 *
 * A card is a picture of a title, so a retitled guide leaves a card that is
 * quietly wrong — the same failure class as a hand-typed README. The manifest
 * records a fingerprint of every input the pixels depend on; this recomputes it
 * from the frontmatter and refuses to pass if anything moved, if a route has no
 * card, or if a card has no route.
 *
 * Fix by running `npm run og` and committing the result.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { readRoutes, REPO_ROOT } from './lib/pages.mjs';
import { assertRenderable, cardInputs, manifestFor } from './lib/og-manifest.mjs';

const MANIFEST = path.join(REPO_ROOT, 'public/og/manifest.json');
const failures = [];

if (!existsSync(MANIFEST)) {
  console.error('public/og/manifest.json is missing. Run `npm run og`.');
  process.exit(1);
}

const cards = cardInputs(readRoutes());
assertRenderable(cards);

const expected = manifestFor(cards);
const actual = JSON.parse(readFileSync(MANIFEST, 'utf8'));

for (const key of ['template', 'width', 'height', 'default']) {
  if (actual[key] !== expected[key]) {
    failures.push(`manifest ${key} is ${JSON.stringify(actual[key])}, expected ${JSON.stringify(expected[key])}`);
  }
}

const actualImages = actual.images ?? {};
for (const [image, entry] of Object.entries(expected.images)) {
  const found = actualImages[image];
  if (!found) {
    failures.push(`${entry.route ?? 'the site default'} has no card in the manifest (${image})`);
    continue;
  }
  if (found.fingerprint !== entry.fingerprint) {
    failures.push(`${image} was drawn from different text than the frontmatter now holds`);
  }
  const file = path.join(REPO_ROOT, 'public', image.replace(/^\//, ''));
  if (!existsSync(file)) failures.push(`${image} is in the manifest but the file is not committed`);
}

for (const image of Object.keys(actualImages)) {
  if (!expected.images[image]) failures.push(`${image} is committed but no route needs it`);
}

if (failures.length) {
  console.error(failures.join('\n'));
  console.error('\nRun `npm run og` and commit public/og/.');
  process.exit(1);
}

console.log(`og check passed: ${Object.keys(expected.images).length} cards match the frontmatter`);

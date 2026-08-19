/**
 * The shape of `public/og/manifest.json`, and the one function that decides
 * what belongs in it. Shared by the generator and the freshness check so the
 * two cannot disagree about what a current set of cards looks like.
 */
import { createHash } from 'node:crypto';
import {
  DEFAULT_OG_CARD,
  DEFAULT_OG_IMAGE,
  OG_IMAGE_WIDTH,
  OG_IMAGE_HEIGHT,
  ogImagePath,
} from '../../src/lib/og.js';

/**
 * Bump when the card template changes shape, so `check:og` notices that every
 * committed card was drawn by an older template and asks for a regeneration.
 */
export const TEMPLATE_VERSION = 1;

/** The site-level fallback, used by any page with no card of its own. */
const DEFAULT_CARD = { route: null, ...DEFAULT_OG_CARD };

/**
 * @typedef {object} Card
 * @property {string | null} route
 * @property {string} image   site-relative path, e.g. `/og/connect-gmail.png`
 * @property {string} eyebrow
 * @property {string} title
 */

/**
 * Every card the site needs: the fallback plus one per route.
 *
 * @param {Array<{ route: string, title: string, eyebrow: string }>} routes
 * @returns {Card[]}
 */
export function cardInputs(routes) {
  return [
    DEFAULT_CARD,
    ...routes.map((page) => ({
      route: page.route,
      image: ogImagePath(page.route),
      eyebrow: page.eyebrow,
      title: page.title,
    })),
  ];
}

/**
 * A card's identity: everything the rendered pixels depend on. The manifest
 * stores this rather than a hash of the PNG, so the check does not depend on
 * the rasteriser producing byte-identical output across platforms.
 *
 * @param {Card} card
 */
function fingerprint(card) {
  const input = JSON.stringify([
    TEMPLATE_VERSION,
    OG_IMAGE_WIDTH,
    OG_IMAGE_HEIGHT,
    card.image,
    card.eyebrow,
    card.title,
  ]);
  return createHash('sha256').update(input).digest('hex').slice(0, 16);
}

/**
 * The codepoints present in the vendored Inter subset under
 * `assets/og/fonts/`. A title using anything outside this set would render as
 * a blank box, which is exactly the failure this whole change exists to remove,
 * so both the generator and the check refuse it up front.
 *
 * Keep in step with the `--unicodes` argument of the subset command recorded in
 * the header of `scripts/generate-og.mjs`.
 */
const SUBSET_RANGES = [
  [0x0020, 0x007e],
  [0x00a0, 0x00ff],
  [0x2010, 0x2015],
  [0x2018, 0x201d],
  [0x2026, 0x2026],
  [0x2192, 0x2192],
];

/**
 * Throws if any card would need a glyph the vendored font subset lacks.
 *
 * @param {Card[]} cards
 */
export function assertRenderable(cards) {
  const missing = new Set();
  for (const card of cards) {
    for (const char of `${card.title}${card.eyebrow}`) {
      const code = char.codePointAt(0);
      if (!SUBSET_RANGES.some(([lo, hi]) => code >= lo && code <= hi)) {
        missing.add(`${char} (U+${code.toString(16).toUpperCase().padStart(4, '0')})`);
      }
    }
  }
  if (missing.size) {
    throw new Error(
      `og card text uses characters the vendored Inter subset does not cover: ${[...missing].join(', ')}. ` +
        'Re-subset the font with a wider --unicodes range, or change the text.'
    );
  }
}

/**
 * @param {Card[]} cards
 */
export function manifestFor(cards) {
  /** @type {Record<string, { route: string | null, fingerprint: string }>} */
  const images = {};
  for (const card of cards) {
    images[card.image] = { route: card.route, fingerprint: fingerprint(card) };
  }
  return {
    generator: 'scripts/generate-og.mjs',
    template: TEMPLATE_VERSION,
    width: OG_IMAGE_WIDTH,
    height: OG_IMAGE_HEIGHT,
    default: DEFAULT_OG_IMAGE,
    images,
  };
}

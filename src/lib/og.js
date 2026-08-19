/**
 * Open Graph card geometry and the route -> file mapping.
 *
 * Plain ESM rather than TypeScript on purpose: this module is the single source
 * of truth for where a page's social card lives, and it is imported by both the
 * Astro build (`src/components/Seo.astro`) and by plain Node scripts
 * (`scripts/generate-og.mjs`, `scripts/check-og.mjs`). A `.ts` file would need a
 * type-stripping loader on the Node side, and CI runs Node 22, so `.js` keeps
 * one file readable by both without a build step in between.
 */

/** Facebook, LinkedIn, Slack and X all render 1.91:1 at this size. */
export const OG_IMAGE_WIDTH = 1200;
export const OG_IMAGE_HEIGHT = 630;

/** Public directory the generated cards are written to and served from. */
export const OG_DIR = '/og';

/** Used by any page that has no card of its own. Always exists. */
export const DEFAULT_OG_IMAGE = `${OG_DIR}/default.png`;

/**
 * The words printed on the fallback card. Kept here so the generator draws them
 * and `Seo.astro` describes them from the same constant — alt text that claims
 * the card says something it does not is worse than no alt text.
 */
export const DEFAULT_OG_CARD = {
  image: DEFAULT_OG_IMAGE,
  eyebrow: 'Playbook',
  title: 'A free reference on connecting AI agents to real systems',
};

/**
 * The card file for a site-relative page path.
 *
 * `/`                  -> /og/home.png
 * `/about/`            -> /og/about.png
 * `/connect/`          -> /og/connect.png
 * `/connect/gmail/`    -> /og/connect-gmail.png
 *
 * @param {string} path
 * @returns {string}
 */
export function ogImagePath(path) {
  const segments = path.split('/').filter(Boolean);
  if (segments.length === 0) return `${OG_DIR}/home.png`;
  return `${OG_DIR}/${segments.join('-')}.png`;
}

/**
 * Alt text for a card. Social clients read this out, and the card is a picture
 * of words, so the alt says what the words are rather than describing a design.
 *
 * @param {string} title
 * @returns {string}
 */
export function ogImageAlt(title) {
  return `Title card reading "${title}", from the Agent Integration Playbook.`;
}

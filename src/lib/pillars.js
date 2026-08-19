/**
 * The three pillars, in the order the site presents them.
 *
 * Plain ESM rather than TypeScript because Node scripts outside the Astro build
 * import this list directly (see `scripts/lib/pages.mjs`), and CI runs Node 22.
 */

/** @typedef {'connect' | 'govern' | 'fail'} PillarSlug */

/**
 * @typedef {object} Pillar
 * @property {PillarSlug} slug
 * @property {string} title
 * @property {string} blurb
 * @property {number} order
 */

/** @type {Pillar[]} */
export const PILLARS = [
  {
    slug: 'connect',
    title: 'Connect',
    blurb: 'Giving an agent real access to Gmail, Slack, GitHub, storage, databases, and MCP servers.',
    order: 1,
  },
  {
    slug: 'govern',
    title: 'Govern',
    blurb: 'Identity, OAuth flows, least-privilege scopes, credential handling, and audit trails.',
    order: 2,
  },
  {
    slug: 'fail',
    title: 'Fail',
    blurb: 'What actually breaks in production, why it breaks, and how to catch it early.',
    order: 3,
  },
];

/**
 * @param {string} slug
 * @returns {Pillar | undefined}
 */
export function getPillar(slug) {
  return PILLARS.find((p) => p.slug === slug);
}

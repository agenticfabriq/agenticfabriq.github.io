/**
 * The site's own origin, in a module plain Node can import.
 *
 * `src/lib/seo.ts` re-exports this so nothing else has to know it moved. It
 * lives on its own because the README generator and the social-card scripts run
 * outside the Astro build and need the same string the pages emit — a second
 * copy of an origin is how a canonical and a shared link end up disagreeing.
 */

/** @type {string} */
export const SITE_URL = 'https://agenticfabriq.github.io';

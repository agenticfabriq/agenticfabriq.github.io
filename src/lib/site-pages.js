/**
 * The two pages that are not guides.
 *
 * Their title and description live here rather than inline in the `.astro`
 * files so that the social-card generator and the README index can read the
 * same strings the pages render. Plain ESM for the same reason as
 * `src/lib/og.js`: Node scripts import it directly.
 */

/** @typedef {{ path: string, title: string, description: string, eyebrow: string }} SitePage */

/** @type {SitePage} */
export const HOME_PAGE = {
  path: '/',
  title: 'Agent Integration Playbook',
  description:
    'An open reference for connecting AI agents to real systems: tool integrations, OAuth and identity, least privilege, audit trails, and production failure modes.',
  eyebrow: 'The library',
};

/** @type {SitePage} */
export const ABOUT_PAGE = {
  path: '/about/',
  title: 'About the Agent Integration Playbook',
  description:
    'Who publishes the Agent Integration Playbook, the editorial standard every guide is held to, what the build refuses to ship, and how to report an error.',
  eyebrow: 'About',
};

/** @type {SitePage[]} */
export const SITE_PAGES = [HOME_PAGE, ABOUT_PAGE];

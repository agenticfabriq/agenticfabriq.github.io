/**
 * Reads the guide collection straight from the Markdown frontmatter, so that
 * scripts outside the Astro build (the social-card generator, its freshness
 * check, and the README index) see exactly what the site sees.
 *
 * The Astro content collection is not reachable from a plain Node process, so
 * this parses the same files with the same YAML parser Astro uses.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fg from 'fast-glob';
import yaml from 'js-yaml';
import { PILLARS } from '../../src/lib/pillars.js';
import { SITE_PAGES } from '../../src/lib/site-pages.js';

export const REPO_ROOT = path.resolve(fileURLToPath(new URL('../..', import.meta.url)));

const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---/;

/**
 * @typedef {object} Guide
 * @property {string} id           `connect/gmail`, or `connect/index` for a cornerstone
 * @property {string} pillar       `connect` | `govern` | `fail`
 * @property {string} slug         `gmail`, or `index` for a cornerstone
 * @property {boolean} cornerstone
 * @property {string} title
 * @property {string} description
 * @property {string} route        site-relative, always trailing-slashed
 */

/**
 * Every guide, ordered cornerstone-first within each pillar and pillars in
 * their declared order — the same order the home page renders.
 *
 * @returns {Guide[]}
 */
export function readGuides() {
  const base = path.join(REPO_ROOT, 'src/content/guides');
  const files = fg.sync('**/*.md', { cwd: base }).sort();
  const guides = files.map((file) => {
    const source = readFileSync(path.join(base, file), 'utf8');
    const match = FRONTMATTER.exec(source);
    if (!match) throw new Error(`${file} has no frontmatter`);
    const data = yaml.load(match[1]);
    const id = file.replace(/\.md$/, '');
    const [pillar, slug] = id.split('/');
    if (!slug) throw new Error(`${file} is not under a pillar directory`);
    return {
      id,
      pillar,
      slug,
      cornerstone: data.cornerstone === true,
      title: String(data.title),
      description: String(data.description),
      route: slug === 'index' ? `/${pillar}/` : `/${pillar}/${slug}/`,
    };
  });

  const pillarOrder = new Map(PILLARS.map((p, index) => [p.slug, index]));
  return guides.sort((a, b) => {
    const byPillar = (pillarOrder.get(a.pillar) ?? 99) - (pillarOrder.get(b.pillar) ?? 99);
    if (byPillar !== 0) return byPillar;
    if (a.cornerstone !== b.cornerstone) return a.cornerstone ? -1 : 1;
    return a.slug.localeCompare(b.slug);
  });
}

/**
 * @typedef {object} SiteRoute
 * @property {string} route     site-relative, trailing-slashed
 * @property {string} title
 * @property {string} eyebrow   the small line above the title on a social card
 */

/**
 * Every route the site builds: the two standalone pages plus every guide.
 *
 * @returns {SiteRoute[]}
 */
export function readRoutes() {
  const pillarTitle = new Map(PILLARS.map((p) => [p.slug, p.title]));
  const standalone = SITE_PAGES.map((page) => ({
    route: page.path,
    title: page.title,
    eyebrow: page.eyebrow,
  }));
  const guides = readGuides().map((guide) => ({
    route: guide.route,
    title: guide.title,
    eyebrow: guide.cornerstone
      ? `${pillarTitle.get(guide.pillar) ?? guide.pillar} — cornerstone`
      : (pillarTitle.get(guide.pillar) ?? guide.pillar),
  }));
  return [...standalone, ...guides];
}

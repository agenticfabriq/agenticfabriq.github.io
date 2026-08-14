import { readFileSync } from 'node:fs';
import fg from 'fast-glob';
import * as cheerio from 'cheerio';

const MIN_INTERNAL_LINKS = 3;
const files = await fg('dist/**/index.html');
const routes = new Set(files.map((file) => '/' + file.replace(/^dist\//, '').replace(/index\.html$/, '')));
const failures = [];
const linkedTo = new Set(['/']);

for (const file of files) {
  const route = '/' + file.replace(/^dist\//, '').replace(/index\.html$/, '');
  const $ = cheerio.load(readFileSync(file, 'utf8'));
  const internal = [];

  $('a[href]').each((_, element) => {
    const href = $(element).attr('href');
    if (!href || /^(https?:|mailto:|#)/.test(href)) return;
    const target = href.endsWith('/') ? href : `${href}/`;
    internal.push(target);
    linkedTo.add(target);
    if (!routes.has(target)) failures.push(`${route} -> broken internal link ${href}`);
  });

  const distinct = new Set(internal.filter((target) => target !== route && target !== '/'));
  if (distinct.size < MIN_INTERNAL_LINKS) {
    failures.push(`${route} has ${distinct.size} distinct internal links, needs ${MIN_INTERNAL_LINKS}`);
  }
}

for (const route of routes) {
  if (!linkedTo.has(route)) failures.push(`${route} is orphaned, nothing links to it`);
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log(`link check passed: ${routes.size} routes`);

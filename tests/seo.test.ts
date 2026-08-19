import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import fg from 'fast-glob';
import * as cheerio from 'cheerio';

const pages: Array<{ file: string; $: cheerio.CheerioAPI }> = [];

beforeAll(async () => {
  if (!existsSync('dist')) throw new Error('run `npm run build` before the seo suite');
  const files = await fg('dist/**/index.html');
  for (const file of files) {
    pages.push({ file, $: cheerio.load(readFileSync(file, 'utf8')) });
  }
});

describe('built pages', () => {
  it('produced at least one page', () => {
    expect(pages.length).toBeGreaterThan(0);
  });

  it('gives every page a non-empty title of at most 60 characters', () => {
    for (const { file, $ } of pages) {
      const title = $('title').text();
      expect(title.length, file).toBeGreaterThan(0);
      expect(title.length, file).toBeLessThanOrEqual(60);
    }
  });

  it('gives every page a unique title', () => {
    const titles = pages.map((p) => p.$('title').text());
    expect(new Set(titles).size).toBe(titles.length);
  });

  it('gives every page a unique meta description of 140-160 characters', () => {
    const descriptions: string[] = [];
    for (const { file, $ } of pages) {
      const description = $('meta[name="description"]').attr('content') ?? '';
      expect(description.length, file).toBeGreaterThanOrEqual(140);
      expect(description.length, file).toBeLessThanOrEqual(160);
      descriptions.push(description);
    }
    expect(new Set(descriptions).size).toBe(descriptions.length);
  });

  it('gives every page an absolute canonical url', () => {
    for (const { file, $ } of pages) {
      const href = $('link[rel="canonical"]').attr('href') ?? '';
      expect(href.startsWith('https://agenticfabriq.github.io/'), file).toBe(true);
      expect(href.endsWith('/'), file).toBe(true);
    }
  });

  it('gives every page open graph and twitter tags', () => {
    for (const { file, $ } of pages) {
      expect($('meta[property="og:title"]').attr('content'), file).toBeTruthy();
      expect($('meta[property="og:url"]').attr('content'), file).toBeTruthy();
      expect($('meta[name="twitter:card"]').attr('content'), file).toBe('summary_large_image');
    }
  });

  it('emits only parseable json-ld', () => {
    for (const { file, $ } of pages) {
      const blocks = $('script[type="application/ld+json"]');
      expect(blocks.length, file).toBeGreaterThan(0);
      blocks.each((_, element) => {
        const parsed = JSON.parse($(element).text());
        expect(parsed['@context'], file).toBe('https://schema.org');
        expect(parsed['@type'], file).toBeTruthy();
      });
    }
  });

  it('has exactly one h1 per page', () => {
    for (const { file, $ } of pages) {
      expect($('h1').length, file).toBe(1);
    }
  });

  it('ships no client-side javascript', () => {
    for (const { file, $ } of pages) {
      const scripts = $('script').filter((_, element) => $(element).attr('type') !== 'application/ld+json');
      expect(scripts.length, file).toBe(0);
    }
  });

  it('gives every image an alt attribute', () => {
    for (const { file, $ } of pages) {
      $('img').each((_, element) => {
        expect($(element).attr('alt'), file).toBeDefined();
      });
    }
  });
});

/** Every JSON-LD block on a page, parsed. */
function blocks($: cheerio.CheerioAPI): Array<Record<string, any>> {
  return $('script[type="application/ld+json"]')
    .map((_, element) => JSON.parse($(element).text()))
    .get();
}

/** Every Organization node anywhere in a page's JSON-LD, at any depth. */
function organizations(value: unknown, found: Array<Record<string, any>> = []): Array<Record<string, any>> {
  if (Array.isArray(value)) {
    for (const item of value) organizations(item, found);
    return found;
  }
  if (value && typeof value === 'object') {
    const node = value as Record<string, any>;
    if (node['@type'] === 'Organization') found.push(node);
    for (const key of Object.keys(node)) organizations(node[key], found);
  }
  return found;
}

describe('publisher identity', () => {
  it('describes the publisher as one organisation across the whole site', () => {
    const seen = new Map<string, string>();
    let total = 0;
    for (const { file, $ } of pages) {
      for (const org of organizations(blocks($))) {
        total += 1;
        expect(org['@id'], file).toBe('https://agenticfabriq.com/#organization');
        // A top-level block carries `@context`; the same entity embedded as a
        // publisher does not. Compare the entity, not the wrapper.
        const { '@context': _context, ...entity } = org;
        const shape = JSON.stringify(entity);
        const first = seen.get(org['@id']);
        if (first === undefined) seen.set(org['@id'], shape);
        else expect(shape, `${file} disagrees with an earlier page about the organisation`).toBe(first);
      }
    }
    expect(total).toBeGreaterThan(0);
    expect(seen.size).toBe(1);
  });

  it('gives every organisation node a resolvable logo, url and sameAs', () => {
    for (const { file, $ } of pages) {
      for (const org of organizations(blocks($))) {
        expect(org.name, file).toBe('Agentic Fabriq');
        expect(org.url, file).toBe('https://agenticfabriq.com');
        expect(org.description, file).toBeTruthy();
        expect(org.logo['@type'], file).toBe('ImageObject');
        expect(org.logo.url.startsWith('https://'), file).toBe(true);
        expect(org.sameAs, file).toContain('https://github.com/agenticfabriq');
        expect(org.sameAs, file).toContain('https://agenticfabriq.com');
      }
    }
  });

  it('names no author person anywhere in the structured data', () => {
    for (const { file, $ } of pages) {
      for (const block of blocks($)) {
        expect(JSON.stringify(block), file).not.toContain('"@type":"Person"');
      }
    }
  });
});

describe('article structured data', () => {
  const articlesOf = ($: cheerio.CheerioAPI) => blocks($).filter((b) => b['@type'] === 'Article');

  it('emits exactly one Article block on every guide page', () => {
    const guides = pages.filter(({ file }) => file !== 'dist/index.html' && file !== 'dist/about/index.html');
    expect(guides.length).toBeGreaterThan(0);
    for (const { file, $ } of guides) {
      expect(articlesOf($).length, file).toBe(1);
    }
  });

  it('sets dateModified to an ISO date on every Article', () => {
    for (const { file, $ } of pages) {
      for (const article of articlesOf($)) {
        expect(article.dateModified, file).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      }
    }
  });

  it('shows the same date on the page as the Article reports as dateModified', () => {
    for (const { file, $ } of pages) {
      for (const article of articlesOf($)) {
        const shown = $('main').text().match(/Updated (\d{4}-\d{2}-\d{2})/);
        expect(shown, `${file} renders no visible Updated date`).toBeTruthy();
        expect(shown![1], file).toBe(article.dateModified);
      }
    }
  });

  it('anchors every Article to the page canonical and to the site', () => {
    for (const { file, $ } of pages) {
      const canonicalUrl = $('link[rel="canonical"]').attr('href');
      for (const article of articlesOf($)) {
        expect(article.url, file).toBe(canonicalUrl);
        expect(article.mainEntityOfPage, file).toBe(canonicalUrl);
        expect(article['@id'], file).toBe(`${canonicalUrl}#article`);
        expect(article.isPartOf['@id'], file).toBe('https://agenticfabriq.github.io/#website');
      }
    }
  });

  it('gives every Article an organisation as both author and publisher', () => {
    for (const { file, $ } of pages) {
      for (const article of articlesOf($)) {
        expect(article.author['@type'], file).toBe('Organization');
        expect(article.publisher['@type'], file).toBe('Organization');
        expect(article.publisher.logo.url, file).toBeTruthy();
      }
    }
  });
});


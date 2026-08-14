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

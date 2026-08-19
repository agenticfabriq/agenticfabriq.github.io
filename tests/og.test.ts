import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import fg from 'fast-glob';
import * as cheerio from 'cheerio';
import { DEFAULT_OG_IMAGE, OG_IMAGE_WIDTH, OG_IMAGE_HEIGHT } from '../src/lib/og.js';

const SITE = 'https://agenticfabriq.github.io';

const pages: Array<{ file: string; $: cheerio.CheerioAPI }> = [];

beforeAll(async () => {
  if (!existsSync('dist')) throw new Error('run `npm run build` before the og suite');
  const files = await fg('dist/**/index.html');
  for (const file of files) {
    pages.push({ file, $: cheerio.load(readFileSync(file, 'utf8')) });
  }
});

const og = ($: cheerio.CheerioAPI, property: string) =>
  $(`meta[property="${property}"]`).attr('content');
const twitter = ($: cheerio.CheerioAPI, name: string) => $(`meta[name="${name}"]`).attr('content');

/** The site-relative path a built page's absolute image URL points at. */
function imagePath(url: string): string {
  return url.slice(SITE.length);
}

/** Width and height straight out of a PNG's IHDR chunk. */
function pngSize(file: string): { width: number; height: number } {
  const bytes = readFileSync(file);
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  expect(bytes.subarray(0, 8).equals(signature), `${file} is not a PNG`).toBe(true);
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

describe('open graph images', () => {
  it('found the built pages', () => {
    expect(pages.length).toBeGreaterThan(0);
  });

  it('gives every page an og:image on an absolute https url on this site', () => {
    for (const { file, $ } of pages) {
      const url = og($, 'og:image') ?? '';
      expect(url.startsWith(`${SITE}/og/`), `${file}: ${url}`).toBe(true);
      expect(url.endsWith('.png'), file).toBe(true);
    }
  });

  it('declares the dimensions and the mime type of every og:image', () => {
    for (const { file, $ } of pages) {
      expect(og($, 'og:image:width'), file).toBe(String(OG_IMAGE_WIDTH));
      expect(og($, 'og:image:height'), file).toBe(String(OG_IMAGE_HEIGHT));
      expect(og($, 'og:image:type'), file).toBe('image/png');
    }
  });

  it('gives every og:image alt text naming the page it belongs to', () => {
    for (const { file, $ } of pages) {
      const alt = og($, 'og:image:alt') ?? '';
      const title = $('title').text();
      expect(alt.length, file).toBeGreaterThan(0);
      expect(alt, file).toContain(title);
    }
  });

  it('points twitter at the same image, with the same alt text', () => {
    for (const { file, $ } of pages) {
      expect(twitter($, 'twitter:image'), file).toBe(og($, 'og:image'));
      expect(twitter($, 'twitter:image:alt'), file).toBe(og($, 'og:image:alt'));
      // The card type is what makes a missing image render as an empty box, so
      // it stays asserted alongside the image it now promises.
      expect(twitter($, 'twitter:card'), file).toBe('summary_large_image');
    }
  });

  it('ships every referenced image in dist, at the dimensions it claims', () => {
    for (const { file, $ } of pages) {
      const target = `dist${imagePath(og($, 'og:image') ?? '')}`;
      expect(existsSync(target), `${file} references ${target}, which was not built`).toBe(true);
      expect(pngSize(target), file).toEqual({ width: OG_IMAGE_WIDTH, height: OG_IMAGE_HEIGHT });
    }
  });

  it('gives each page its own card rather than one shared picture', () => {
    const urls = pages.map(({ $ }) => og($, 'og:image'));
    expect(new Set(urls).size, 'two pages share a card').toBe(urls.length);
  });

  it('never leaves a page on the site-level fallback', () => {
    // The fallback exists so a page added before `npm run og` runs still emits a
    // resolvable image. No page should be sitting on it in a shipped build.
    for (const { file, $ } of pages) {
      expect(imagePath(og($, 'og:image') ?? ''), `${file} fell back to the default card`).not.toBe(
        DEFAULT_OG_IMAGE
      );
    }
  });

  it('ships the fallback card itself, so the fallback is never a 404', () => {
    const target = `dist${DEFAULT_OG_IMAGE}`;
    expect(existsSync(target)).toBe(true);
    expect(pngSize(target)).toEqual({ width: OG_IMAGE_WIDTH, height: OG_IMAGE_HEIGHT });
  });
});

describe('the og manifest', () => {
  const manifest = () => JSON.parse(readFileSync('dist/og/manifest.json', 'utf8'));

  it('is published alongside the cards', () => {
    expect(existsSync('dist/og/manifest.json')).toBe(true);
  });

  it('agrees with the tags every page emits', () => {
    const images = manifest().images as Record<string, { route: string | null }>;
    const byRoute = new Map(
      Object.entries(images)
        .filter(([, entry]) => entry.route !== null)
        .map(([image, entry]) => [entry.route as string, image])
    );
    expect(byRoute.size).toBe(pages.length);
    for (const { file, $ } of pages) {
      const route = `/${file.replace(/^dist\//, '').replace(/index\.html$/, '')}`;
      expect(byRoute.get(route), `${file} has no manifest entry`).toBe(
        imagePath(og($, 'og:image') ?? '')
      );
    }
  });

  it('records the dimensions the pages claim', () => {
    expect(manifest().width).toBe(OG_IMAGE_WIDTH);
    expect(manifest().height).toBe(OG_IMAGE_HEIGHT);
  });
});

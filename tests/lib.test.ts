import { describe, it, expect } from 'vitest';
import { SITE_URL, canonical, articleJsonLd, breadcrumbJsonLd, organizationJsonLd } from '../src/lib/seo';

describe('canonical', () => {
  it('builds an absolute url with a trailing slash', () => {
    expect(canonical('/connect/gmail/')).toBe(`${SITE_URL}/connect/gmail/`);
  });

  it('adds a missing trailing slash', () => {
    expect(canonical('/connect/gmail')).toBe(`${SITE_URL}/connect/gmail/`);
  });

  it('returns the bare site url for the root', () => {
    expect(canonical('/')).toBe(`${SITE_URL}/`);
  });

  it('never doubles slashes', () => {
    expect(canonical('//connect//gmail//')).toBe(`${SITE_URL}/connect/gmail/`);
  });
});

describe('articleJsonLd', () => {
  const ld = articleJsonLd({
    title: 'Connecting an AI Agent to Gmail',
    description: 'desc',
    url: `${SITE_URL}/connect/gmail/`,
    updated: '2026-08-14',
    keywords: ['a', 'b'],
  }) as Record<string, unknown>;

  it('declares the Article type', () => {
    expect(ld['@type']).toBe('Article');
  });

  it('carries the schema.org context', () => {
    expect(ld['@context']).toBe('https://schema.org');
  });

  it('uses the url as mainEntityOfPage', () => {
    expect(ld.mainEntityOfPage).toBe(`${SITE_URL}/connect/gmail/`);
  });

  it('serialises keywords as a comma separated string', () => {
    expect(ld.keywords).toBe('a, b');
  });

  it('sets dateModified to the updated date', () => {
    expect(ld.dateModified).toBe('2026-08-14');
  });
});

describe('breadcrumbJsonLd', () => {
  const ld = breadcrumbJsonLd([
    { name: 'Playbook', url: `${SITE_URL}/` },
    { name: 'Connect', url: `${SITE_URL}/connect/` },
  ]) as Record<string, any>;

  it('declares the BreadcrumbList type', () => {
    expect(ld['@type']).toBe('BreadcrumbList');
  });

  it('numbers positions from one', () => {
    expect(ld.itemListElement[0].position).toBe(1);
    expect(ld.itemListElement[1].position).toBe(2);
  });
});

describe('organizationJsonLd', () => {
  it('points at the agenticfabriq domain', () => {
    const ld = organizationJsonLd() as Record<string, unknown>;
    expect(ld.url).toBe('https://agenticfabriq.com');
  });
});

import { describe, it, expect } from 'vitest';
import {
  SITE_URL,
  PARENT_URL,
  GITHUB_ORG_URL,
  REPO_URL,
  ISSUES_URL,
  ORG_LOGO_URL,
  ORG_ID,
  WEBSITE_ID,
  canonical,
  articleJsonLd,
  aboutPageJsonLd,
  breadcrumbJsonLd,
  organizationJsonLd,
  organizationNode,
  websiteJsonLd,
  websiteNode,
  howToJsonLd,
  faqJsonLd,
} from '../src/lib/seo';

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

describe('howToJsonLd', () => {
  const ld = howToJsonLd({
    title: 'Connecting an AI Agent to Gmail',
    description: 'desc',
    url: `${SITE_URL}/connect/gmail/`,
    steps: [
      { name: 'Create OAuth credentials', text: 'Register the agent in Google Cloud console.' },
      { name: 'Scope the consent screen', text: 'Request only the Gmail scopes the agent needs.' },
    ],
  }) as Record<string, any>;

  it('declares the HowTo type', () => {
    expect(ld['@type']).toBe('HowTo');
  });

  it('carries the schema.org context', () => {
    expect(ld['@context']).toBe('https://schema.org');
  });

  it('numbers step positions from one', () => {
    expect(ld.step[0].position).toBe(1);
    expect(ld.step[1].position).toBe(2);
  });
});

describe('faqJsonLd', () => {
  const ld = faqJsonLd([
    { question: 'Does Gmail access require a Workspace admin?', answer: 'Only for domain-wide delegation; individual OAuth consent does not.' },
    { question: 'Can the agent send email without read access?', answer: 'Yes, the gmail.send scope does not grant read access.' },
  ]) as Record<string, any>;

  it('declares the FAQPage type', () => {
    expect(ld['@type']).toBe('FAQPage');
  });

  it('carries the schema.org context', () => {
    expect(ld['@context']).toBe('https://schema.org');
  });

  it('builds a Question mainEntity with an Answer', () => {
    expect(ld.mainEntity[0]['@type']).toBe('Question');
    expect(ld.mainEntity[0].name).toBe('Does Gmail access require a Workspace admin?');
    expect(ld.mainEntity[0].acceptedAnswer['@type']).toBe('Answer');
    expect(ld.mainEntity[0].acceptedAnswer.text).toBe(
      'Only for domain-wide delegation; individual OAuth consent does not.'
    );
  });

  it('carries every entry through to mainEntity', () => {
    expect(ld.mainEntity.length).toBe(2);
  });
});

describe('organizationNode', () => {
  const node = organizationNode() as Record<string, any>;

  it('is embeddable, carrying no @context of its own', () => {
    expect(node['@context']).toBeUndefined();
  });

  it('declares the Organization type under a stable @id', () => {
    expect(node['@type']).toBe('Organization');
    expect(node['@id']).toBe(ORG_ID);
    expect(ORG_ID.startsWith(PARENT_URL)).toBe(true);
  });

  it('carries a logo hosted on the publisher’s own domain', () => {
    expect(node.logo['@type']).toBe('ImageObject');
    expect(node.logo.url).toBe(ORG_LOGO_URL);
    expect(new URL(ORG_LOGO_URL).hostname.endsWith('agenticfabriq.com')).toBe(true);
    expect(node.logo.width).toBeGreaterThan(0);
    expect(node.logo.height).toBeGreaterThan(0);
  });

  it('points sameAs at the product site and the github organisation', () => {
    expect(node.sameAs).toContain(PARENT_URL);
    expect(node.sameAs).toContain(GITHUB_ORG_URL);
  });

  it('carries a non-empty description', () => {
    expect(typeof node.description).toBe('string');
    expect(node.description.length).toBeGreaterThan(0);
  });
});

describe('organizationJsonLd as a top-level block', () => {
  it('is the embeddable node plus a schema.org context', () => {
    const ld = organizationJsonLd() as Record<string, any>;
    expect(ld['@context']).toBe('https://schema.org');
    const { '@context': _context, ...rest } = ld;
    expect(rest).toEqual(organizationNode());
  });
});

describe('websiteJsonLd', () => {
  const ld = websiteJsonLd() as Record<string, any>;

  it('declares the WebSite type under a stable @id', () => {
    expect(ld['@context']).toBe('https://schema.org');
    expect(ld['@type']).toBe('WebSite');
    expect(ld['@id']).toBe(WEBSITE_ID);
  });

  it('points at the site root', () => {
    expect(ld.url).toBe(`${SITE_URL}/`);
  });

  it('names the same organisation as its publisher', () => {
    expect(ld.publisher).toEqual(organizationNode());
  });
});

describe('websiteNode', () => {
  it('is embeddable and shares the WebSite @id', () => {
    const node = websiteNode() as Record<string, any>;
    expect(node['@context']).toBeUndefined();
    expect(node['@id']).toBe(WEBSITE_ID);
    expect(node['@type']).toBe('WebSite');
  });
});

describe('articleJsonLd identity', () => {
  const ld = articleJsonLd({
    title: 'Connecting an AI Agent to Gmail',
    description: 'desc',
    url: `${SITE_URL}/connect/gmail/`,
    updated: '2026-08-14',
    keywords: ['a', 'b'],
  }) as Record<string, any>;

  it('names the article with an @id derived from its url', () => {
    expect(ld['@id']).toBe(`${SITE_URL}/connect/gmail/#article`);
    expect(ld.url).toBe(`${SITE_URL}/connect/gmail/`);
  });

  it('uses one organisation for both author and publisher', () => {
    expect(ld.author).toEqual(organizationNode());
    expect(ld.publisher).toEqual(organizationNode());
    expect(ld.author['@id']).toBe(ORG_ID);
    expect(ld.publisher['@id']).toBe(ORG_ID);
  });

  it('embeds the publisher in full rather than by reference alone', () => {
    expect(ld.publisher.name).toBe('Agentic Fabriq');
    expect(ld.publisher.logo.url).toBe(ORG_LOGO_URL);
  });

  it('places the article inside the site', () => {
    expect(ld.isPartOf['@id']).toBe(WEBSITE_ID);
    expect(ld.isPartOf['@type']).toBe('WebSite');
  });

  it('invents no author person', () => {
    expect(ld.author['@type']).toBe('Organization');
    expect(JSON.stringify(ld)).not.toContain('"Person"');
  });
});

describe('aboutPageJsonLd', () => {
  const ld = aboutPageJsonLd({
    title: 'About the Agent Integration Playbook',
    description: 'desc',
    url: `${SITE_URL}/about/`,
    updated: '2026-08-19',
  }) as Record<string, any>;

  it('declares the AboutPage type with the schema.org context', () => {
    expect(ld['@context']).toBe('https://schema.org');
    expect(ld['@type']).toBe('AboutPage');
    expect(ld['@id']).toBe(`${SITE_URL}/about/#about`);
  });

  it('sets dateModified from the updated date', () => {
    expect(ld.dateModified).toBe('2026-08-19');
  });

  it('is about the publishing organisation and part of the site', () => {
    expect(ld.about['@id']).toBe(ORG_ID);
    expect(ld.isPartOf['@id']).toBe(WEBSITE_ID);
  });

  it('names the same organisation as its publisher', () => {
    expect(ld.publisher).toEqual(organizationNode());
  });
});

describe('repository constants', () => {
  it('derives the issue tracker from the repository url', () => {
    expect(ISSUES_URL).toBe(`${REPO_URL}/issues`);
  });

  it('keeps the repository inside the github organisation', () => {
    expect(REPO_URL.startsWith(`${GITHUB_ORG_URL}/`)).toBe(true);
  });
});

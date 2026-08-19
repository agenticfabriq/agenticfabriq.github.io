export const SITE_URL = 'https://agenticfabriq.github.io';
export const PARENT_URL = 'https://agenticfabriq.com';
export const SITE_NAME = 'Agent Integration Playbook';

/** The publisher's GitHub organisation. Verified to resolve. */
export const GITHUB_ORG_URL = 'https://github.com/agenticfabriq';
/** The repository this site is built from. */
export const REPO_URL = 'https://github.com/agenticfabriq/agenticfabriq.github.io';
export const ISSUES_URL = `${REPO_URL}/issues`;
/**
 * The publisher's own logo, served from the publisher's own site. The www host
 * is used deliberately: the apex redirects, and a structured-data consumer that
 * does not follow redirects should still resolve the image.
 */
export const ORG_LOGO_URL = 'https://www.agenticfabriq.com/fabriq-logo.png';
export const CONTENT_LICENSE_URL = 'https://creativecommons.org/licenses/by/4.0/';

/**
 * Stable node identifiers. Every page that mentions the organisation or the
 * site emits the same `@id`, so a consumer merging the site's JSON-LD sees one
 * organisation and one website rather than one near-duplicate per page.
 */
export const ORG_ID = `${PARENT_URL}/#organization`;
export const WEBSITE_ID = `${SITE_URL}/#website`;

export function canonical(path: string): string {
  const cleaned = `/${path.split('/').filter(Boolean).join('/')}`;
  return cleaned === '/' ? `${SITE_URL}/` : `${SITE_URL}${cleaned}/`;
}

/**
 * The organisation as an embeddable node, with no `@context` of its own.
 * Embedded in full rather than referenced by `@id` alone, because a consumer
 * that reads one page in isolation — which is what a rich-result validator
 * does — still needs the publisher's name and logo on that page.
 */
export function organizationNode() {
  return {
    '@type': 'Organization',
    '@id': ORG_ID,
    name: 'Agentic Fabriq',
    url: PARENT_URL,
    description: 'Secure hub for agent identity, governance, and visibility.',
    logo: {
      '@type': 'ImageObject',
      url: ORG_LOGO_URL,
      width: 1024,
      height: 1024,
    },
    sameAs: [PARENT_URL, GITHUB_ORG_URL],
  };
}

export function organizationJsonLd() {
  return { '@context': 'https://schema.org', ...organizationNode() };
}

/** The site as an embeddable node, for `isPartOf` on individual pages. */
export function websiteNode() {
  return {
    '@type': 'WebSite',
    '@id': WEBSITE_ID,
    name: SITE_NAME,
    url: `${SITE_URL}/`,
    inLanguage: 'en',
  };
}

export function websiteJsonLd() {
  return {
    '@context': 'https://schema.org',
    ...websiteNode(),
    description:
      'An open reference for connecting AI agents to real systems: tool integrations, OAuth and identity, least privilege, audit trails, and production failure modes.',
    license: CONTENT_LICENSE_URL,
    publisher: organizationNode(),
  };
}

export interface ArticleInput {
  title: string;
  description: string;
  url: string;
  updated: string;
  keywords: string[];
}

export function articleJsonLd(input: ArticleInput) {
  return {
    '@context': 'https://schema.org',
    '@type': 'Article',
    '@id': `${input.url}#article`,
    headline: input.title,
    description: input.description,
    url: input.url,
    mainEntityOfPage: input.url,
    dateModified: input.updated,
    keywords: input.keywords.join(', '),
    inLanguage: 'en',
    license: CONTENT_LICENSE_URL,
    isPartOf: websiteNode(),
    author: organizationNode(),
    publisher: organizationNode(),
  };
}

export interface AboutPageInput {
  title: string;
  description: string;
  url: string;
  updated: string;
}

export function aboutPageJsonLd(input: AboutPageInput) {
  return {
    '@context': 'https://schema.org',
    '@type': 'AboutPage',
    '@id': `${input.url}#about`,
    name: input.title,
    description: input.description,
    url: input.url,
    mainEntityOfPage: input.url,
    dateModified: input.updated,
    inLanguage: 'en',
    license: CONTENT_LICENSE_URL,
    isPartOf: websiteNode(),
    about: { '@id': ORG_ID },
    publisher: organizationNode(),
  };
}

export function breadcrumbJsonLd(trail: Array<{ name: string; url: string }>) {
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: trail.map((crumb, index) => ({
      '@type': 'ListItem',
      position: index + 1,
      name: crumb.name,
      item: crumb.url,
    })),
  };
}

export interface HowToInput {
  title: string;
  description: string;
  url: string;
  steps: Array<{ name: string; text: string }>;
}

export function howToJsonLd(input: HowToInput) {
  return {
    '@context': 'https://schema.org',
    '@type': 'HowTo',
    name: input.title,
    description: input.description,
    mainEntityOfPage: input.url,
    step: input.steps.map((step, index) => ({
      '@type': 'HowToStep',
      position: index + 1,
      name: step.name,
      text: step.text,
    })),
  };
}

export function faqJsonLd(entries: Array<{ question: string; answer: string }>) {
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: entries.map((entry) => ({
      '@type': 'Question',
      name: entry.question,
      acceptedAnswer: {
        '@type': 'Answer',
        text: entry.answer,
      },
    })),
  };
}

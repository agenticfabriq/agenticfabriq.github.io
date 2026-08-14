export const SITE_URL = 'https://agenticfabriq.github.io';
export const PARENT_URL = 'https://agenticfabriq.com';

export function canonical(path: string): string {
  const cleaned = `/${path.split('/').filter(Boolean).join('/')}`;
  return cleaned === '/' ? `${SITE_URL}/` : `${SITE_URL}${cleaned}/`;
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
    headline: input.title,
    description: input.description,
    mainEntityOfPage: input.url,
    dateModified: input.updated,
    keywords: input.keywords.join(', '),
    inLanguage: 'en',
    license: 'https://creativecommons.org/licenses/by/4.0/',
    author: { '@type': 'Organization', name: 'Agentic Fabriq', url: PARENT_URL },
    publisher: { '@type': 'Organization', name: 'Agentic Fabriq', url: PARENT_URL },
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

export function organizationJsonLd() {
  return {
    '@context': 'https://schema.org',
    '@type': 'Organization',
    name: 'Agentic Fabriq',
    url: PARENT_URL,
    description: 'Secure hub for agent identity, governance, and visibility.',
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

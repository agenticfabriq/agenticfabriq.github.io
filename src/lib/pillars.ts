export type PillarSlug = 'connect' | 'govern' | 'fail';

export interface Pillar {
  slug: PillarSlug;
  title: string;
  blurb: string;
  order: number;
}

export const PILLARS: Pillar[] = [
  {
    slug: 'connect',
    title: 'Connect',
    blurb: 'Giving an agent real access to Gmail, Slack, GitHub, storage, databases, and MCP servers.',
    order: 1,
  },
  {
    slug: 'govern',
    title: 'Govern',
    blurb: 'Identity, OAuth flows, least-privilege scopes, credential handling, and audit trails.',
    order: 2,
  },
  {
    slug: 'fail',
    title: 'Fail',
    blurb: 'What actually breaks in production, why it breaks, and how to catch it early.',
    order: 3,
  },
];

export function getPillar(slug: string): Pillar | undefined {
  return PILLARS.find((p) => p.slug === slug);
}

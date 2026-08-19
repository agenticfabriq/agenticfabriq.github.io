import { getCollection } from 'astro:content';
import { PILLARS } from '../lib/pillars';
import { canonical } from '../lib/seo';

export async function GET() {
  const guides = await getCollection('guides');
  const homeUrl = canonical('/');
  const lines: string[] = [
    '# Agent Integration Playbook',
    '',
    '> Open reference on connecting AI agents to real systems: tool integrations, OAuth and identity, least-privilege scopes, audit trails, and production failure modes. Prose is CC BY 4.0.',
    '',
    `- [Agent Integration Playbook](${homeUrl}): An open reference for connecting AI agents to real systems: tool integrations, OAuth and identity, least privilege, audit trails, and production failure modes.`,
    `- [About the Agent Integration Playbook](${canonical('/about/')}): Who publishes the library, the editorial standard every guide is held to, what the build refuses to ship, how to report an error, and what goes stale first.`,
    '',
  ];

  for (const pillar of PILLARS) {
    lines.push(`## ${pillar.title}`, '');
    for (const guide of guides.filter((g) => g.data.pillar === pillar.slug)) {
      const url = canonical(`/${guide.id.replace(/\/index$/, '')}/`);
      lines.push(`- [${guide.data.title}](${url}): ${guide.data.description}`);
    }
    lines.push('');
  }

  return new Response(lines.join('\n'), {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

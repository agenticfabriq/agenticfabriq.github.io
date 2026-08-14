import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import { SITE_URL } from '../lib/seo';

export async function GET() {
  const guides = await getCollection('guides');
  return rss({
    title: 'Agent Integration Playbook',
    description: 'Guides on connecting AI agents to real systems safely.',
    site: SITE_URL,
    items: guides.map((guide) => ({
      title: guide.data.title,
      description: guide.data.description,
      link: `/${guide.id.replace(/\/index$/, '')}/`,
      pubDate: new Date(guide.data.updated),
    })),
  });
}

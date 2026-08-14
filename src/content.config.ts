import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { guideFrontmatterSchema } from './lib/guide-schema';

const guides = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/guides' }),
  schema: guideFrontmatterSchema,
});

export const collections = { guides };

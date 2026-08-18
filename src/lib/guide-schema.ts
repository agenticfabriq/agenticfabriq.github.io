import { z } from 'astro/zod';

export const guideFrontmatterSchema = z.object({
  title: z.string().min(10).max(60),
  description: z.string().min(140).max(160),
  pillar: z.enum(['connect', 'govern', 'fail']),
  updated: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  keywords: z.array(z.string().min(3)).min(2).max(8),
  relatedGuides: z.array(z.string().regex(/^(connect|govern|fail)\/[a-z0-9-]+$/)).min(3),
  cornerstone: z.boolean().default(false),
  howTo: z
    .array(z.object({ name: z.string(), text: z.string() }))
    .min(2)
    .optional(),
  faq: z
    .array(z.object({ question: z.string(), answer: z.string() }))
    .min(2)
    .optional(),
});

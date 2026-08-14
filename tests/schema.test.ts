import { describe, it, expect } from 'vitest';
import { guideFrontmatterSchema } from '../src/lib/guide-schema';

const valid = {
  title: 'Connecting an AI Agent to Gmail',
  description: 'A 150 character description that is long enough to satisfy the minimum length rule enforced by the schema for meta descriptions shown in search results.',
  pillar: 'connect',
  updated: '2026-08-14',
  keywords: ['ai agent gmail', 'gmail api oauth'],
  relatedGuides: ['govern/oauth-flows', 'fail/over-scoped-oauth', 'connect/slack'],
};

describe('guideFrontmatterSchema', () => {
  it('accepts a complete guide frontmatter', () => {
    expect(() => guideFrontmatterSchema.parse(valid)).not.toThrow();
  });

  it('rejects a missing description', () => {
    const { description, ...rest } = valid;
    expect(() => guideFrontmatterSchema.parse(rest)).toThrow();
  });

  it('rejects a description shorter than 140 characters', () => {
    expect(() => guideFrontmatterSchema.parse({ ...valid, description: 'too short' })).toThrow();
  });

  it('rejects a description longer than 160 characters', () => {
    expect(() => guideFrontmatterSchema.parse({ ...valid, description: 'x'.repeat(161) })).toThrow();
  });

  it('rejects a title longer than 60 characters', () => {
    expect(() => guideFrontmatterSchema.parse({ ...valid, title: 'x'.repeat(61) })).toThrow();
  });

  it('rejects fewer than three related guides', () => {
    expect(() => guideFrontmatterSchema.parse({ ...valid, relatedGuides: ['connect/slack'] })).toThrow();
  });

  it('defaults cornerstone to false', () => {
    expect(guideFrontmatterSchema.parse(valid).cornerstone).toBe(false);
  });
});

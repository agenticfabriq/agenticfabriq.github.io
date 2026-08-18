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

  it('accepts frontmatter with howTo and faq present and valid', () => {
    const withExtras = {
      ...valid,
      howTo: [
        { name: 'Create OAuth credentials', text: 'Register the agent in Google Cloud console.' },
        { name: 'Scope the consent screen', text: 'Request only the Gmail scopes the agent needs.' },
      ],
      faq: [
        { question: 'Does Gmail access require a Workspace admin?', answer: 'Only for domain-wide delegation.' },
        { question: 'Can the agent send email without read access?', answer: 'Yes, gmail.send does not grant read access.' },
      ],
    };
    expect(() => guideFrontmatterSchema.parse(withExtras)).not.toThrow();
  });

  it('accepts frontmatter with howTo and faq absent', () => {
    expect(() => guideFrontmatterSchema.parse(valid)).not.toThrow();
  });

  it('rejects a howTo with fewer than two entries', () => {
    const withShortHowTo = {
      ...valid,
      howTo: [{ name: 'Create OAuth credentials', text: 'Register the agent in Google Cloud console.' }],
    };
    expect(() => guideFrontmatterSchema.parse(withShortHowTo)).toThrow();
  });

  it('rejects a faq with fewer than two entries', () => {
    const withShortFaq = {
      ...valid,
      faq: [{ question: 'Does Gmail access require a Workspace admin?', answer: 'Only for domain-wide delegation.' }],
    };
    expect(() => guideFrontmatterSchema.parse(withShortFaq)).toThrow();
  });
});

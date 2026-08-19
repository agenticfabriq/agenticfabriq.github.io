import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { readGuides } from '../scripts/lib/pages.mjs';
import { beginMarker, checkCount, endMarker, renderReadme } from '../scripts/lib/readme.mjs';
import { canonical } from '../src/lib/seo';

const README = readFileSync('README.md', 'utf8');
const guides = readGuides();

describe('the generated readme index', () => {
  it('reads every guide out of the frontmatter', () => {
    expect(guides.length).toBe(18);
    expect(guides.filter((guide) => guide.cornerstone).length).toBe(3);
  });

  it('matches what the committed README says', () => {
    // The same assertion `npm run check:readme` makes, made where a failing
    // suite names it: the README is a ranking surface, so a stale one ships a
    // defect rather than an untidy file.
    expect(renderReadme(README, guides)).toBe(README);
  });

  it('lists every guide exactly once, with its frontmatter title and description', () => {
    for (const guide of guides) {
      const link = `- [${guide.title}](${canonical(guide.route)})`;
      expect(README, `${guide.id} is missing or retitled in README.md`).toContain(link);
      expect(README.split(link).length - 1, `${guide.id} is listed twice`).toBe(1);
      // Descriptions wrap across lines, so compare on collapsed whitespace.
      expect(README.replace(/\s+/g, ' ')).toContain(guide.description.replace(/\s+/g, ' '));
    }
  });

  it('marks the cornerstones and only the cornerstones', () => {
    expect(README.split('(cornerstone)').length - 1).toBe(3);
    for (const guide of guides.filter((entry) => entry.cornerstone)) {
      expect(README).toContain(`](${canonical(guide.route)}) (cornerstone)`);
    }
  });

  it('links every guide at an absolute url on the site', () => {
    for (const guide of guides) {
      expect(canonical(guide.route).startsWith('https://agenticfabriq.github.io/')).toBe(true);
    }
  });

  it('rewrites a hand-edited title back to the frontmatter', () => {
    const stale = README.replace(guides[0].title, 'Some Older Title Nobody Updated');
    expect(stale).not.toBe(README);
    expect(renderReadme(stale, guides)).toBe(README);
  });

  it('rewrites a dropped guide back into the list', () => {
    const stale = README.replace(new RegExp(`- \\[${guides[1].title.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}\\][^\\n]*\\n`), '');
    expect(stale).not.toBe(README);
    expect(renderReadme(stale, guides)).toBe(README);
  });

  it('leaves everything outside the markers alone', () => {
    const preserved = [
      '# Agent Integration Playbook',
      '**Read it at [agenticfabriq.github.io](https://agenticfabriq.github.io).**',
      'Prose is CC BY 4.0. Code is Apache-2.0. Maintained by',
      '## Runnable examples',
      '## Contributing',
      '## Local development',
      'npm run verify',
    ];
    const rendered = renderReadme(README.replace(guides[0].title, 'Stale'), guides);
    for (const fragment of preserved) expect(rendered).toContain(fragment);
  });

  it('refuses a README whose markers have been removed', () => {
    const unmarked = README.replace(beginMarker('govern'), '');
    expect(() => renderReadme(unmarked, guides)).toThrow(/govern markers/);
    expect(() => renderReadme(README.replace(endMarker('fail'), ''), guides)).toThrow(/fail markers/);
  });

  it('catches the guide count in the opening sentence going stale', () => {
    expect(checkCount(README, guides.length)).toBeNull();
    expect(checkCount(README, guides.length + 1)).toMatch(/README.md says "Eighteen guides"/);
    expect(checkCount('no such sentence here', guides.length)).toMatch(/no longer states a guide count/);
  });
});

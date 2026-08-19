/**
 * Renders the generated regions of README.md from the guide frontmatter.
 *
 * Pure functions over strings, so the CLI in `scripts/readme-index.mjs` and the
 * suite in `tests/readme.test.ts` exercise the same code rather than two
 * descriptions of it.
 */
import { PILLARS } from '../../src/lib/pillars.js';
import { SITE_URL } from '../../src/lib/site-urls.js';

/** Where a bullet's continuation lines wrap. Matches the prose around it. */
const WRAP = 92;

const NUMBER_WORDS = [
  'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
  'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
  'seventeen', 'eighteen', 'nineteen', 'twenty', 'twenty-one', 'twenty-two',
  'twenty-three', 'twenty-four', 'twenty-five', 'twenty-six', 'twenty-seven',
  'twenty-eight', 'twenty-nine', 'thirty',
];

/** @param {string} pillar */
export const beginMarker = (pillar) =>
  `<!-- BEGIN GENERATED ${pillar}: edit src/content/guides/, then run \`npm run readme\` -->`;

/** @param {string} pillar */
export const endMarker = (pillar) => `<!-- END GENERATED ${pillar} -->`;

/**
 * Greedy wrap that never breaks a word, so the output is stable under
 * regeneration and reads like the hand-written prose around it.
 *
 * @param {string} text
 * @param {number} width
 * @param {string} indent
 */
function wrap(text, width, indent) {
  const lines = [];
  let line = '';
  for (const word of text.split(/\s+/)) {
    if (line === '') line = word;
    else if (`${indent}${line} ${word}`.length > width) {
      lines.push(indent + line);
      line = word;
    } else line = `${line} ${word}`;
  }
  if (line !== '') lines.push(indent + line);
  return lines;
}

/** @param {import('./pages.mjs').Guide} guide */
export function bullet(guide) {
  const label = guide.cornerstone ? ' (cornerstone)' : '';
  const head = `- [${guide.title}](${SITE_URL}${guide.route})${label}`;
  return [head, ...wrap(`— ${guide.description}`, WRAP, '  ')].join('\n');
}

/**
 * Replaces each pillar's marked region with the current guide list, leaving
 * every other byte of the file alone.
 *
 * @param {string} text     the README as it stands
 * @param {import('./pages.mjs').Guide[]} guides
 * @returns {string}
 * @throws if a pillar's markers are missing or inverted
 */
export function renderReadme(text, guides) {
  let out = text;
  for (const pillar of PILLARS) {
    const begin = beginMarker(pillar.slug);
    const end = endMarker(pillar.slug);
    const start = out.indexOf(begin);
    const stop = out.indexOf(end);
    if (start === -1 || stop === -1 || stop < start) {
      throw new Error(
        `README.md is missing the ${pillar.slug} markers. Expected a region delimited by:\n  ${begin}\n  ${end}`
      );
    }
    const body = guides
      .filter((guide) => guide.pillar === pillar.slug)
      .map(bullet)
      .join('\n');
    out = `${out.slice(0, start)}${begin}\n\n${body}\n\n${out.slice(stop)}`;
  }
  return out;
}

/**
 * The opening states the guide count in words. It is the same mirror as the
 * index, one sentence earlier, so it is held to the same standard — but it sits
 * inside hand-written prose, so this reports rather than rewrites.
 *
 * @param {string} text
 * @param {number} count
 * @returns {string | null} a message when the sentence is wrong, else null
 */
export function checkCount(text, count) {
  const expected = NUMBER_WORDS[count];
  if (!expected) return `there is no word for ${count} guides; extend NUMBER_WORDS in scripts/lib/readme.mjs`;
  const stated = /\b([A-Za-z-]+|\d+) guides, no gate, no signup\./.exec(text.replace(/\n\s+/g, ' '));
  if (!stated) return 'README.md no longer states a guide count ("<N> guides, no gate, no signup.")';
  if (stated[1].toLowerCase() === expected) return null;
  const capitalised = `${expected[0].toUpperCase()}${expected.slice(1)}`;
  return (
    `README.md says "${stated[1]} guides" but src/content/guides/ holds ${count} ` +
    `(expected "${capitalised}"). Fix the sentence by hand.`
  );
}

/**
 * A line-by-line account of how a README differs from what it should be.
 *
 * @param {string} want
 * @param {string} have
 * @returns {string[]}
 */
export function drift(want, have) {
  const wanted = want.split('\n');
  const actual = have.split('\n');
  const lines = [];
  for (let i = 0; i < Math.max(wanted.length, actual.length); i += 1) {
    if (wanted[i] !== actual[i]) {
      lines.push(
        `  README.md:${i + 1}\n    is:     ${actual[i] ?? '(end of file)'}\n    should: ${wanted[i] ?? '(end of file)'}`
      );
    }
  }
  return lines;
}

/**
 * Generates one Open Graph card per route into `public/og/`, plus a site-level
 * default, plus `public/og/manifest.json`.
 *
 * The cards are committed static assets. Nothing runs at request time: the site
 * is static, hosted on GitHub Pages, and ships no client JavaScript. Run this
 * after changing a guide title, a pillar name, or the template below:
 *
 *     npm run og
 *
 * `npm run check:og` fails the build when the committed cards no longer match
 * the frontmatter, so a stale card cannot ship silently.
 *
 * Text is rendered with Inter, vendored under `assets/og/fonts/` with its OFL
 * licence. Nothing is fetched from a remote host, at build time or at any other
 * time. Satori converts glyphs to SVG paths, so the rasteriser needs no fonts
 * of its own and the output does not depend on what is installed on the
 * machine that runs this.
 *
 * The vendored files are Inter 4.1 statics (`extras/ttf/` of the rsms/inter
 * release), subset with fonttools to keep them at ~39 kB each:
 *
 *     pyftsubset Inter-Regular.ttf --output-file=Inter-Regular.subset.ttf \
 *       --unicodes="U+0020-007E,U+00A0-00FF,U+2010-2015,U+2018-201D,U+2026,U+2192,U+00D7" \
 *       --layout-features="kern,liga,calt" --no-hinting --desubroutinize
 *
 * `assertRenderable` below fails the run if any card needs a glyph outside that
 * range, so re-subsetting is a loud requirement rather than a silent tofu box.
 */
import { mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import satori from 'satori';
import sharp from 'sharp';
import { OG_IMAGE_WIDTH, OG_IMAGE_HEIGHT, DEFAULT_OG_IMAGE, ogImagePath } from '../src/lib/og.js';
import { readRoutes, REPO_ROOT } from './lib/pages.mjs';
import { assertRenderable, cardInputs, manifestFor, TEMPLATE_VERSION } from './lib/og-manifest.mjs';

const OUT_DIR = path.join(REPO_ROOT, 'public/og');
const FONT_DIR = path.join(REPO_ROOT, 'assets/og/fonts');

/** Site ink, from `src/styles/global.css`. */
const BG = '#16181d';
const EDGE = '#343b47';
const RULE = '#2a303a';
const ACCENT = '#7d9dfb'; // the site accent (#2f5bd7) lifted for contrast on ink
const TITLE = '#ffffff';
const MUTED = '#9aa3b2';

const fonts = [
  { name: 'Inter', data: readFileSync(path.join(FONT_DIR, 'Inter-Regular.subset.ttf')), weight: 400, style: 'normal' },
  { name: 'Inter', data: readFileSync(path.join(FONT_DIR, 'Inter-Bold.subset.ttf')), weight: 700, style: 'normal' },
];

/**
 * The card. Deliberately plain: an accent rule, the pillar, the title, and the
 * site name. A dark card with a light edge stays legible whether the client
 * paints it on white or on near-black chrome, and the title is set large enough
 * to survive the ~360px-wide thumbnail a mobile client renders.
 *
 * @param {{ title: string, eyebrow: string }} card
 */
function template({ title, eyebrow }) {
  // Long titles get a step down so three lines still fit the plate.
  const fontSize = title.length > 46 ? 62 : 72;
  return {
    type: 'div',
    props: {
      style: {
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        backgroundColor: BG,
        border: `2px solid ${EDGE}`,
        fontFamily: 'Inter',
      },
      children: [
        { type: 'div', props: { style: { width: '100%', height: '12px', backgroundColor: ACCENT } } },
        {
          type: 'div',
          props: {
            style: {
              display: 'flex',
              flexDirection: 'column',
              flexGrow: 1,
              justifyContent: 'center',
              padding: '0 76px',
            },
            children: [
              {
                type: 'div',
                props: {
                  style: {
                    fontSize: 26,
                    fontWeight: 700,
                    letterSpacing: 4,
                    textTransform: 'uppercase',
                    color: ACCENT,
                    marginBottom: 26,
                  },
                  children: eyebrow,
                },
              },
              {
                type: 'div',
                props: {
                  style: {
                    fontSize,
                    fontWeight: 700,
                    lineHeight: 1.14,
                    letterSpacing: -1,
                    color: TITLE,
                  },
                  children: title,
                },
              },
            ],
          },
        },
        {
          type: 'div',
          props: {
            style: {
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              margin: '0 76px 54px',
              paddingTop: 28,
              borderTop: `2px solid ${RULE}`,
              fontSize: 26,
              color: MUTED,
            },
            children: [
              { type: 'div', props: { style: { fontWeight: 700, color: '#d7dce5' }, children: 'Agent Integration Playbook' } },
              { type: 'div', props: { children: 'agenticfabriq.github.io' } },
            ],
          },
        },
      ],
    },
  };
}

/** @param {{ title: string, eyebrow: string }} card */
async function render(card) {
  const svg = await satori(template(card), {
    width: OG_IMAGE_WIDTH,
    height: OG_IMAGE_HEIGHT,
    fonts,
  });
  return sharp(Buffer.from(svg))
    .png({ compressionLevel: 9, effort: 10, palette: true })
    .toBuffer();
}

const cards = cardInputs(readRoutes());
assertRenderable(cards);

mkdirSync(OUT_DIR, { recursive: true });
for (const stale of readdirSync(OUT_DIR, { withFileTypes: true })) {
  if (stale.isFile()) rmSync(path.join(OUT_DIR, stale.name));
}

let bytes = 0;
for (const card of cards) {
  const png = await render(card);
  bytes += png.length;
  writeFileSync(path.join(REPO_ROOT, 'public', card.image.replace(/^\//, '')), png);
}

writeFileSync(path.join(OUT_DIR, 'manifest.json'), `${JSON.stringify(manifestFor(cards), null, 2)}\n`);

const kb = Math.round(bytes / 1024);
console.log(
  `generated ${cards.length} og cards (${kb} kB total, template v${TEMPLATE_VERSION}), default ${DEFAULT_OG_IMAGE}, ` +
    `mapping from ${ogImagePath('/')}`
);

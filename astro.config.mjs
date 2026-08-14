import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://agenticfabriq.github.io',
  trailingSlash: 'always',
  build: { format: 'directory', inlineStylesheets: 'always' },
  integrations: [sitemap()],
  markdown: {
    shikiConfig: { theme: 'github-dark', wrap: false },
  },
});

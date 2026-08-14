import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://agenticfabriq.github.io',
  trailingSlash: 'always',
  build: { format: 'directory', inlineStylesheets: 'always' },
  integrations: [
    sitemap({
      serialize(item) {
        item.changefreq = 'monthly';
        item.priority = item.url.split('/').filter(Boolean).length <= 3 ? 0.9 : 0.7;
        return item;
      },
    }),
  ],
  markdown: {
    shikiConfig: { theme: 'github-dark', wrap: false },
  },
});

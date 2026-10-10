import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Project site published at https://docs.tripl.io/
const config: Config = {
  title: 'tripl',
  tagline: 'Keep your product analytics honest.',
  favicon: 'img/logo.svg',

  url: 'https://docs.tripl.io',
  baseUrl: '/',

  organizationName: 'tripl-io',
  projectName: 'tripl',
  trailingSlash: false,

  // Cross-doc links fixed; broken links now fail the build.
  onBrokenLinks: 'throw',

  // .md -> CommonMark (literal braces, so ${var}/{slug} don't break),
  // .mdx -> MDX (for future interactive / OpenAPI pages).
  markdown: {format: 'detect', hooks: {onBrokenMarkdownLinks: 'throw'}},

  i18n: {defaultLocale: 'en', locales: ['en']},

  // Cookie banner and Google Tag Manager, on docs.tripl.io only.
  clientModules: ['./src/clientModules/consent.ts'],

  // Pages that moved keep their old addresses working.
  plugins: [
    [
      '@docusaurus/plugin-client-redirects',
      {
        redirects: [
          {from: '/use-cases/overview', to: '/integrate/agent-api-guide'},
          {from: '/use-cases/llm-agent', to: '/integrate/agent-api-guide'},
          {from: '/use-cases/searching-events', to: '/integrate/searching-from-the-api'},
          {from: '/category/automation--agents', to: '/integrate/agent-api-guide'},
        ],
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/tripl-io/tripl/tree/main/website/',
        },
        blog: false,
        theme: {customCss: './src/css/custom.css'},
      } satisfies Preset.Options,
    ],
    [
      'redocusaurus',
      {
        specs: [
          // Generated, never edited: `make sync-types` writes it together with
          // backend/openapi.json, in the app's own order (Redoc lists
          // operations in document order), and CI fails when the two differ.
          {id: 'tripl-api', spec: 'openapi/tripl.openapi.json', route: '/integrate/api/'},
        ],
        theme: {primaryColor: '#2563eb'},
      },
    ],
  ],

  themeConfig: {
    navbar: {
      title: 'tripl',
      // The logo and title lead to the product site; "Docs" in the navbar
      // stays the way back to the documentation's front page.
      logo: {alt: 'tripl', src: 'img/logo.svg', href: 'https://tripl.io', target: '_self'},
      items: [
        {type: 'docSidebar', sidebarId: 'docsSidebar', position: 'left', label: 'Docs'},
        {to: '/integrate/api/', label: 'API', position: 'left'},
        {href: 'https://github.com/tripl-io/tripl', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {label: 'tripl.io', href: 'https://tripl.io'},
        {label: 'Privacy', href: 'https://tripl.io/privacy/'},
        {
          html: '<button type="button" class="footer__link-item tripl-consent-open" data-consent-open>Cookie settings</button>',
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} tripl. Licensed under AGPL-3.0-or-later.`,
    },
    prism: {theme: prismThemes.github, darkTheme: prismThemes.dracula},
    colorMode: {respectPrefersColorScheme: true},
  } satisfies Preset.ThemeConfig,
};

export default config;

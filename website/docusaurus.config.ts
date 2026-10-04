import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Project site published at https://tripl-io.github.io/tripl/
const config: Config = {
  title: 'tripl',
  tagline: 'Keep your product analytics honest.',
  favicon: 'img/logo.svg',

  url: 'https://tripl-io.github.io',
  baseUrl: '/tripl/',

  organizationName: 'tripl-io',
  projectName: 'tripl',
  trailingSlash: false,

  // Cross-doc links fixed; broken links now fail the build.
  onBrokenLinks: 'throw',

  // .md -> CommonMark (literal braces, so ${var}/{slug} don't break),
  // .mdx -> MDX (for future interactive / OpenAPI pages).
  markdown: {format: 'detect', hooks: {onBrokenMarkdownLinks: 'throw'}},

  i18n: {defaultLocale: 'en', locales: ['en']},

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
          {id: 'tripl-api', spec: 'openapi/tripl.openapi.json', route: '/integrate/api/'},
        ],
        theme: {primaryColor: '#2563eb'},
      },
    ],
  ],

  themeConfig: {
    navbar: {
      title: 'tripl',
      logo: {alt: 'tripl', src: 'img/logo.svg'},
      items: [
        {type: 'docSidebar', sidebarId: 'docsSidebar', position: 'left', label: 'Docs'},
        {to: '/integrate/api/', label: 'API', position: 'left'},
        {href: 'https://github.com/tripl-io/tripl', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [],
      copyright: `Copyright © ${new Date().getFullYear()} tripl. Licensed under AGPL-3.0-or-later.`,
    },
    prism: {theme: prismThemes.github, darkTheme: prismThemes.dracula},
    colorMode: {respectPrefersColorScheme: true},
  } satisfies Preset.ThemeConfig,
};

export default config;

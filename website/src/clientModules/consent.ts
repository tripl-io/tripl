/**
 * Cookie consent and Google Tag Manager for docs.tripl.io, mirroring the
 * tripl.io website (tripl-io/tripl-site, src/components/Consent.astro).
 *
 * The visitor's choice lives in the `tripl_consent` cookie on .tripl.io, so
 * one answer on either site covers both. GTM loads on every visit with every
 * consent type denied (Consent Mode v2); the banner can grant
 * analytics_storage only. The choice also reaches the dataLayer as
 * `analytics_consent` ("granted" | "denied" | "unknown") with the events
 * `consent_ready` on load and `consent_update` on a choice.
 *
 * Runs only on docs.tripl.io: local builds, forks and previews load nothing.
 */
import ExecutionEnvironment from '@docusaurus/ExecutionEnvironment';

const GTM_ID = 'GTM-PCBSH8VS';
const HOST = 'docs.tripl.io';
const COOKIE = 'tripl_consent';
const MAX_AGE = 180 * 24 * 60 * 60;
const PRIVACY_URL = 'https://tripl.io/privacy/';

type Choice = 'granted' | 'denied';

declare global {
  interface Window {
    dataLayer: unknown[];
  }
}

function readChoice(): Choice | null {
  const m = document.cookie.match(new RegExp(`(?:^|; )${COOKIE}=(granted|denied)`));
  return m ? (m[1] as Choice) : null;
}

function saveChoice(v: Choice): void {
  document.cookie = `${COOKIE}=${v}; Max-Age=${MAX_AGE}; Path=/; Domain=tripl.io; SameSite=Lax; Secure`;
}

// Analytics cookies (GA's _ga, _ga_<id>; PostHog's ph_<key>_posthog) are set
// on this host or on .tripl.io; a withdrawal removes them from both.
function clearAnalyticsCookies(): void {
  for (const c of document.cookie.split(';')) {
    const name = c.split('=')[0].trim();
    if (!/^_ga(_|$)|^_gid$|^_gat|^ph_/.test(name)) continue;
    for (const d of ['', location.hostname, '.tripl.io']) {
      document.cookie = `${name}=; Max-Age=0; Path=/${d ? `; Domain=${d}` : ''}`;
    }
  }
}

function buildBanner(onChoice: (v: Choice) => void): HTMLElement {
  const banner = document.createElement('div');
  banner.className = 'tripl-consent';
  banner.setAttribute('role', 'dialog');
  banner.setAttribute('aria-live', 'polite');
  banner.setAttribute('aria-label', 'Cookie consent');
  banner.hidden = true;

  const text = document.createElement('p');
  text.append(
    "We'd like to use analytics cookies to see how these docs are used. None are set unless you accept, and you can change your mind under ",
    Object.assign(document.createElement('strong'), {textContent: 'Cookie settings'}),
    ' at the bottom of the page. See our ',
    Object.assign(document.createElement('a'), {href: PRIVACY_URL, textContent: 'privacy policy'}),
    '.',
  );

  const actions = document.createElement('div');
  actions.className = 'tripl-consent__actions';
  for (const [value, label] of [
    ['denied', 'Reject'],
    ['granted', 'Accept'],
  ] as const) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tripl-consent__btn';
    b.dataset.consentChoice = value;
    b.textContent = label;
    b.addEventListener('click', () => onChoice(value));
    actions.append(b);
  }

  banner.append(text, actions);
  return banner;
}

function start(): void {
  window.dataLayer = window.dataLayer || [];
  // gtag() must push the arguments object itself, as Google's snippet does.
  function gtag(..._args: unknown[]): void {
    // eslint-disable-next-line prefer-rest-params
    window.dataLayer.push(arguments);
  }

  // Order matters: the defaults and the stored choice must be in the
  // dataLayer before the container starts.
  const stored = readChoice();
  gtag('consent', 'default', {
    ad_storage: 'denied',
    ad_user_data: 'denied',
    ad_personalization: 'denied',
    analytics_storage: stored === 'granted' ? 'granted' : 'denied',
  });
  window.dataLayer.push({event: 'consent_ready', analytics_consent: stored ?? 'unknown'});
  window.dataLayer.push({'gtm.start': new Date().getTime(), event: 'gtm.js'});
  const s = document.createElement('script');
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtm.js?id=${GTM_ID}`;
  document.head.append(s);

  const banner = buildBanner((choice) => {
    saveChoice(choice);
    banner.hidden = true;
    gtag('consent', 'update', {analytics_storage: choice});
    window.dataLayer.push({event: 'consent_update', analytics_consent: choice});
    if (choice === 'denied') clearAnalyticsCookies();
  });
  document.body.append(banner);
  if (!stored) banner.hidden = false;

  // The footer's "Cookie settings" button is rendered by React and comes and
  // goes with navigation, so listen on the document rather than on it. CSS
  // shows the button only while this attribute is set (an attribute, not a
  // class: Docusaurus rewrites <html>'s classes on hydration).
  document.documentElement.dataset.triplConsent = 'on';
  document.addEventListener('click', (e) => {
    if (!(e.target as HTMLElement).closest('[data-consent-open]')) return;
    banner.hidden = false;
    banner.querySelector<HTMLElement>('[data-consent-choice]')?.focus();
  });
}

if (ExecutionEnvironment.canUseDOM && location.hostname === HOST) {
  start();
}

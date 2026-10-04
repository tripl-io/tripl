(function () {
  try {
    var stored = localStorage.getItem('tripl-ui-theme')
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    var theme =
      stored === 'light' ? 'light' :
      stored === 'dark' ? 'dark' :
      stored === 'system' ? (prefersDark ? 'dark' : 'light') :
      'dark'
    var root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.style.background = theme === 'dark'
      ? 'oklch(0.14 0.01 250)'
      : 'oklch(0.99 0.002 250)'
    // Accent and density too, before first paint: set only by React after the
    // entry chunk ran, a violet user saw teal and a "comfy" user saw rows jump.
    // Same allow-lists as theme-provider.tsx.
    var accent = localStorage.getItem('tripl-ui-theme-accent')
    if (['teal', 'violet', 'lime', 'amber', 'rose'].indexOf(accent) === -1) accent = 'teal'
    var density = localStorage.getItem('tripl-ui-theme-density')
    if (['compact', 'cozy', 'comfy'].indexOf(density) === -1) density = 'compact'
    root.classList.add('accent-' + accent, 'density-' + density)
  } catch (e) { /* ignore — default styles take over */ }
})()

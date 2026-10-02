/* Theme resolution, and nothing else.

   FPL ships two themes and keys them off an attribute (`.ism[data-theme=…]`);
   so does style.css. Resolving the attribute here rather than in a
   prefers-color-scheme media query is what lets the dark map in that file
   exist exactly once — a second copy under a media query is the usual way to
   do this, and the usual way for the two to drift apart.

   This has to run before the first paint, which is why it is its own file in
   the <head> of all three page shells rather than part of app.js: a reader on
   the dark theme should never see a white page appear and then correct itself.
   It is also why the setup pages, which are standalone and do not load app.js,
   still load this.

   data-theme-source records whose decision the theme was. app.js reads it to
   decide whether it may still follow the OS: once the reader has pressed the
   switch, the OS changing its mind is no longer an instruction. */
(function () {
  'use strict';

  var KEY = 'fpl-helper-theme';
  var stored = null;
  /* Private-mode Safari throws on access rather than returning null. */
  try { stored = window.localStorage.getItem(KEY); } catch (e) {}

  var explicit = stored === 'light' || stored === 'dark';
  var theme = explicit
    ? stored
    : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

  var root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-theme-source', explicit ? 'stored' : 'system');
})();

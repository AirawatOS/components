// App entry — an ES MODULE (no build step). It imports each screen from its own file and wires the nav.
// AOS + AI18n are globals (loaded as classic <script> before this module), so screens just use window.AOS.
import { accountsScreen } from './screens/accounts.js';
import { aboutScreen } from './screens/about.js';

const SCREENS = { accounts: accountsScreen, about: aboutScreen };

AOS.boot(() => {
  const nav = [...document.querySelectorAll('.nav a')];
  const go = t => { nav.forEach(a => a.classList.toggle('on', a.dataset.t === t)); (SCREENS[t] || SCREENS.accounts)(); };
  nav.forEach(a => a.addEventListener('click', () => go(a.dataset.t)));
  go('accounts');
});

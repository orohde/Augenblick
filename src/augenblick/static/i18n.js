/* Augenblick! – languages.

   German is the language the programme is written in: every text stands in the code and in the page as it reads
   in German, and that German text is also the key under which the other languages keep their wording
   (i18n-en.js, i18n-fr.js, i18n-it.js). A text nobody has translated yet simply stays German.

   Numbered gaps – {0}, {1} – are filled in by the caller; a translation may move them around. */
'use strict';

const I18N = {};                       // language -> {German text: translated text}
const LANGS = ['de', 'en', 'fr', 'it'];

// as in Ordnung!: the account's choice (remembered in this browser), else the browser language
const LANG = (() => {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('augenblick.lang')); } catch { /* private mode or nothing stored */ }
  if (LANGS.includes(saved)) return saved;
  const wanted = (navigator.language || 'de').slice(0, 2).toLowerCase();
  return LANGS.includes(wanted) ? wanted : 'en';
})();
const LOCALE = { de: 'de-DE', en: 'en-GB', fr: 'fr-FR', it: 'it-IT' }[LANG];     // for dates and numbers

const t = (text, values) => {
  const table = I18N[LANG];
  let out = (table && table[text]) || (LANG !== 'de' && I18N.en && I18N.en[text]) || text;       // untranslated: English, then German
  if (values) out = out.replace(/\{(\d+)\}/g, (gap, n) => (n in values ? values[n] : gap));
  return out;
};

/* The page itself (index.html) is written in German. Once, before anything else touches it, its texts are
   swapped for the chosen language. A paragraph with nothing but plain formatting inside (bold, code, a link)
   is one text, markup included, so a translation can order its words freely; anything that holds parts the
   programme fills in later is translated piece by piece, and those parts are left alone.
   An element with translate="no" is left as it is; data-i18n gives the text of an element another key
   than its German wording, where that wording is not enough to tell what is meant.
   With `collect` (a Set) nothing is changed and the keys are gathered instead – that is how the list for the
   translators is made (tools/i18n/keys.py). */
const I18N_ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
const I18N_INLINE = new Set(['B', 'I', 'EM', 'STRONG', 'CODE', 'KBD', 'BR', 'A', 'SPAN', 'SMALL', 'U']);
const squeeze = (text) => text.replace(/\s+/g, ' ').trim();

function applyI18n(root = document.documentElement, collect = null) {
  const table = I18N[LANG];
  if (!table && !collect) return;
  const swap = (key) => (collect ? (collect.add(key), null) : table[key] || null);
  const plain = (node) => [...node.children].every((child) => I18N_INLINE.has(child.tagName) && !child.id && !(child.tagName === 'SPAN' && child.className)
    && !child.querySelector('[id], input, select, textarea, button') && plain(child));
  const walk = (node) => {
    if (node.tagName === 'SCRIPT' || node.tagName === 'STYLE' || node.getAttribute('translate') === 'no') return;
    for (const name of I18N_ATTRS) {
      const value = node.getAttribute(name);
      if (value && /\p{L}{2}/u.test(value)) { const to = swap(squeeze(value)); if (to) node.setAttribute(name, to); }
    }
    const texts = [...node.childNodes].filter((child) => child.nodeType === 3 && /\p{L}{2}/u.test(child.nodeValue));
    if (texts.length && node.children.length && plain(node)) {
      const to = swap(squeeze(node.innerHTML));
      if (to) node.innerHTML = to;
      return;
    }
    for (const text of texts) {
      // one German word can mean two things elsewhere (Ordner: one folder, several folders): data-i18n names which
      const to = swap(node.dataset.i18n || squeeze(text.nodeValue));
      if (to) text.nodeValue = text.nodeValue.replace(/^(\s*)[\s\S]*?(\s*)$/, (all, lead, tail) => lead + to + tail);
    }
    for (const child of node.children) walk(child);
  };
  walk(root);
  if (!collect) document.documentElement.lang = LANG;
}

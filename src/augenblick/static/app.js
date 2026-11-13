/* Augenblick! – web UI. Plain JavaScript, no build step. Texts: see i18n.js. */
'use strict';

applyI18n();          // the page is written in German; before anything else, put it into the chosen language

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
};

const store = {
  get(key, fallback) {
    try { const raw = localStorage.getItem('augenblick.' + key); return raw === null ? fallback : JSON.parse(raw); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem('augenblick.' + key, JSON.stringify(value)); } catch { /* private mode */ } },
};

async function api(path, options = {}) {
  const res = await fetch('api/' + path, {
    credentials: 'same-origin',
    headers: { 'X-Language': LANG, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    ...options,
  });
  if (res.status === 401 && !options.quiet) { showLogin(); throw new Error('unauthorized'); }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const AREA_COLORS = ['#0070f2', '#c35500', '#188918', '#a100c2', '#d20a0a', '#007c87', '#5d36ff', '#8f6a00'];
const number = (n) => n.toLocaleString(LOCALE);
const tn = (n, one, many, more = []) => t(n === 1 ? one : many, [number(n), ...more]);      // a sentence for one and one for many
// what a longer job is about, as the server names it: a count of pictures or faces, or a name
const jobWhat = (what) => { const m = /^(\d+) (Bilder|Gesichter)$/.exec(what || ''); return m ? t(m[2] === 'Bilder' ? '{0} Bilder' : '{0} Gesichter', [number(Number(m[1]))]) : what; };

const State = {
  me: null,
  areas: [],                 // [{name, count}]
  active: new Set(),         // selected areas
  inbox: false,              // looking at the inbox instead of the collection
  folder: '',
  album: '',                 // chosen album, instead of a folder
  auto: '',                  // … or one of the albums that fill themselves
  autos: [],
  memories: null,            // journeys and looks back, as the server found them; null = not asked yet
  memory: '',                // the one chosen
  memList: false,            // all of them, as cards
  memPicked: true,           // of the one chosen: a few pictures that stand for it, or all
  person: '',                // … or everybody's photos of one person
  people: [],                // [{name, count}]
  misfit: false,
  albums: [],
  recursive: store.get('recursive', true),
  q: '', camera: '', place: '', kind: '', noPlace: false, noDate: false,
  tree: [],
  open: new Set(store.get('open', [])),
  openAlbums: new Set(store.get('openAlbums', [])),
  months: [],                // [{month, count}], newest first
  items: new Map(),          // month -> [{key, kind, taken}]
  generation: 0,             // rises with every reload, so late answers are dropped
  scanning: false,
  tile: 150,                 // wished edge of a picture in the overview, px – remembered per user and folder
  view: 'grid',              // grid | map
  selecting: false,
  selected: new Set(),       // keys
  dirty: false,              // something was changed while the viewer was open
  stale: false,              // … or while another page was shown
  ai: { enabled: false, prompts: [], default: '', excluded_areas: [], memories: false },
};

// ----------------------------------------------------------------- theme

const THEMES = ['system', 'light', 'dark'];
let theme = store.get('theme', 'system');
function applyTheme() {
  const dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  $('theme-dark').disabled = !dark;
  $('theme-light').disabled = dark;
  $('btn-theme').textContent = { system: '◐', light: '☀', dark: '☾' }[theme];
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
$('btn-theme').addEventListener('click', () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  store.set('theme', theme);
  applyTheme();
});

// ----------------------------------------------------------------- login

$('acc-lang').value = LANG;
$('acc-lang').addEventListener('change', async () => {
  const language = $('acc-lang').value;
  try { await api('me/prefs', { method: 'PUT', body: JSON.stringify({ language }) }); } catch { /* keep local */ }
  store.set('lang', language);
  location.reload();
});

async function showLogin() {
  $('view-app').hidden = true;
  $('viewer').hidden = true;
  $('view-login').hidden = false;
  let setup = { needed: false, home: true };
  try { setup = await api('setup', { quiet: true }); } catch { /* show the login form */ }
  $('setup-form').hidden = !setup.needed;
  $('login-form').hidden = setup.needed;
  if (setup.needed && !setup.home) {
    $('setup-error').textContent = t('Die erste Einrichtung geht nur aus dem Heimnetz.');
    $('setup-error').hidden = false;
  }
  (setup.needed ? $('setup-user') : $('login-user')).focus();
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('login-error');
  error.hidden = true;
  try {
    await api('login', { method: 'POST', quiet: true, body: JSON.stringify({
      username: $('login-user').value, password: $('login-pass').value, code: $('login-code').value || null }) });
    $('login-pass').value = '';
    $('login-code').value = '';
    $('login-code-box').hidden = true;
    start();
  } catch (err) {
    if (err.message === 'code required') {
      $('login-code-box').hidden = false;
      $('login-code').focus();
      return;
    }
    error.textContent = err.status === 429 ? t('Zu viele Fehlversuche. Bitte später noch einmal.')
      : err.message === 'invalid code' ? t('Der Code stimmt nicht.') : t('Anmeldung fehlgeschlagen.');
    error.hidden = false;
  }
});

$('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('setup-error');
  error.hidden = true;
  if ($('setup-pass').value !== $('setup-pass2').value) {
    error.textContent = t('Die beiden Passwörter sind verschieden.');
    error.hidden = false;
    return;
  }
  try {
    await api('setup', { method: 'POST', quiet: true, body: JSON.stringify({
      username: $('setup-user').value, password: $('setup-pass').value, display_name: $('setup-name').value }) });
    $('setup-pass').value = $('setup-pass2').value = '';
    start();
  } catch (err) {
    error.textContent = err.message.includes('too short') ? t('Das Passwort braucht mindestens 12 Zeichen.')
      : err.message.includes('invalid user name') ? t('Der Benutzername darf nur Kleinbuchstaben, Ziffern, Punkt, Strich enthalten.')
      : t('Das hat nicht geklappt: {0}', [err.message]);
    error.hidden = false;
  }
});

$('btn-logout').addEventListener('click', async () => {
  try { await api('logout', { method: 'POST', quiet: true }); } catch { /* gone anyway */ }
  showLogin();
});

// ----------------------------------------------------------------- start

async function start() {
  try { State.me = await api('me'); } catch { return; }
  // the language belongs to the account: chosen once, it is the same on every device
  if (LANGS.includes(State.me.language) && State.me.language !== LANG) {
    store.set('lang', State.me.language);       // the account's language wins
    if (store.get('lang', null) === State.me.language) { location.reload(); return; }
  }
  $('view-login').hidden = true;
  $('view-app').hidden = false;
  document.querySelectorAll('.admin-only').forEach((node) => { node.hidden = !State.me.is_admin; });
  $('btn-select').hidden = !State.me.write.length;
  $('misfit-box').hidden = $('sel-sort').hidden = !State.me.sorting;
  if (!State.me.sorting) { State.misfit = false; $('chk-misfit').checked = false; }
  State.areas = await api('areas');
  const remembered = store.get('areas', null);
  const names = shelfNames();
  State.active = new Set(remembered ? names.filter((n) => remembered.includes(n)) : names);
  if (!State.active.size) State.active = new Set(names);
  $('chk-recursive').checked = State.recursive;
  route();
  renderChips();
  applyView();
  loadTrashCount();
  loadAiOffer();
  loadFaceSearch();
  await Promise.all([loadTree(), loadCameras(), loadPlaceNames(), reload()]);
  pollStatus();
}

// The inbox is the area the phone uploads to. Its pictures are not part of the collection yet:
// they show behind the button "Eingang" only, until they are moved into another area.
const inboxNames = () => State.areas.filter((a) => a.inbox).map((a) => a.name);
const shelfNames = () => State.areas.filter((a) => !a.inbox).map((a) => a.name);
const inInbox = (folder) => Boolean(folder) && inboxNames().includes(folder.split('/')[0]);
function keepShelf() {        // the chosen areas never include the inbox, and never nothing
  const shelf = shelfNames();
  for (const name of [...State.active]) if (!shelf.includes(name)) State.active.delete(name);
  if (!State.active.size) shelf.forEach((name) => State.active.add(name));
}
function showInbox() {
  const names = inboxNames();
  const count = State.areas.filter((a) => a.inbox).reduce((sum, a) => sum + a.count, 0);
  $('btn-inbox').hidden = !names.length;
  $('btn-inbox').classList.toggle('is-on', State.inbox);
  $('inbox-badge').hidden = !count;
  $('inbox-badge').textContent = number(count);
  if (State.inboxCount !== undefined && State.inboxCount !== count && State.inbox && !$('view-library').hidden && $('viewer').hidden && !State.selected.size) {
    loadTree(); reload(true);       // something arrived or left while the inbox is on screen
  }
  State.inboxCount = count;
}

function areaQuery() {
  return (State.inbox ? inboxNames() : [...State.active]).join(',');
}

function query() {
  const params = new URLSearchParams({ areas: areaQuery() });
  if (State.folder) params.set('folder', State.folder);
  if (!State.recursive) params.set('recursive', 'false');
  if (State.q) params.set('q', State.q);
  if (State.camera) params.set('camera', State.camera);
  if (State.place) {      // "country" or "country|town"
    const [country, city] = State.place.split('|');
    params.set('country', country);
    if (city) params.set('city', city);
  }
  if (State.kind) params.set('kind', State.kind);
  if (State.noPlace) params.set('no_place', 'true');
  if (State.noDate) params.set('no_date', 'true');
  if (State.misfit) params.set('misfit', 'true');
  if (State.album) params.set('album', State.album);
  if (State.auto) params.set('auto', State.auto);
  const memory = shownMemory();
  if (memory) {
    params.set('since', memory.since); params.set('until', memory.until);
    if (State.memPicked) params.set('keys', memory.picks.join(','));
  }
  if (State.person) params.set('person', State.person);
  return params.toString();
}

function renderChips() {
  keepShelf();
  showInbox();
  fillMoveTargets();
  const box = $('area-chips');
  box.replaceChildren();
  box.hidden = !State.areas.length || State.inbox;
  State.areas.forEach((area, i) => {
    if (area.inbox) return;
    box.append(el('button', {
      class: 'chip', type: 'button', 'aria-pressed': String(State.active.has(area.name)),
      style: `--dot:${AREA_COLORS[i % AREA_COLORS.length]}`,
      onclick: () => {
        if (State.active.has(area.name)) State.active.delete(area.name); else State.active.add(area.name);
        if (!State.active.size) State.active.add(area.name);
        store.set('areas', [...State.active]);
        if (State.folder && !State.active.has(State.folder.split('/')[0])) State.folder = '';
        renderChips();
        loadTree(); loadCameras(); loadPlaceNames(); reload();
      },
    }, el('span', { class: 'chip__dot' }), area.name));
  });
}

// ----------------------------------------------------------------- tree

async function loadTree() {
  State.tree = await api('tree?areas=' + encodeURIComponent(areaQuery()));
  renderTree();
  loadAlbums();
  loadPeople();
}

async function loadPeople() {
  try { State.people = await api('people?areas=' + encodeURIComponent(areaQuery())); } catch { return; }
  renderPeople();
}

const personHref = (name) => here() + '/person/' + encodeURIComponent(name);
function renderPeople() {
  showPeopleBox();
  $('people').replaceChildren(...State.people.map((person) => el('a', {
    class: 'tree-row' + (person.name === State.person ? ' is-selected' : ''), href: personHref(person.name),
    role: 'treeitem', 'aria-selected': String(person.name === State.person),
    oncontextmenu: (event) => { event.preventDefault(); personMenu(person, event.clientX, event.clientY); },
    onclick: () => $('tree-panel').classList.remove('is-open'),
  }, person.face ? el('img', { class: 'tree-avatar', src: `api/faces/${person.face}/thumb`, alt: '', loading: 'lazy' })
    : el('span', { class: 'tree-caret is-leaf', 'aria-hidden': 'true' }, '›'),
  el('span', { class: 'tree-name', title: person.name }, person.name),
  el('span', { class: 'tree-count' }, number(person.count)))));
  $('people-names').replaceChildren(...State.people.map((person) => el('option', { value: person.name })));
}

async function loadAlbums() {
  loadAuto();
  try { State.albums = await api('albums?areas=' + encodeURIComponent(areaQuery())); } catch { return; }
  renderAlbums();
}

// Albums that fill themselves: films, Live Photos, panoramas … – the server says which hold anything.
async function loadAuto() {
  loadMemories();
  // the server names the categories in German; here they get the name of the chosen language
  const names = { videos: t('Videos'), live: t('Live Photos'), panoramen: t('Panoramen'), favoriten: t('Favoriten'), raw: t('RAW'), heute: t('An diesem Tag') };
  try {
    State.autos = (await api('auto?areas=' + encodeURIComponent(areaQuery()), { quiet: true })).map((entry) => ({ ...entry, name: names[entry.id] || entry.name }));
  } catch { return; }
  renderAuto();
}

// Memories: journeys and looks back the server finds in the photos. Each is a stretch of days.
// The API key opens everything, so it is not shown until asked for – a look over the shoulder
// or a picture of the screen should not give it away. Copying works without showing it.
const ApiKey = { token: '', shown: false };
function paintApiKey() {
  const { token, shown } = ApiKey;
  $('api-key').value = shown ? token : '•'.repeat(Math.min(token.length, 43));
  $('api-key').placeholder = token ? '' : '–';
  $('api-eye').hidden = !token;
  $('api-eye').setAttribute('aria-pressed', String(shown));
  $('api-eye').setAttribute('aria-label', shown ? t('Schlüssel verbergen') : t('Schlüssel anzeigen'));
  $('api-eye').classList.toggle('is-on', shown);
  $('api-example').textContent = `curl -H "Authorization: Bearer ${token && shown ? token : '…'}" \\\n     "${location.origin}/api/timeline"`;
}
$('api-eye').addEventListener('click', () => { ApiKey.shown = !ApiKey.shown; paintApiKey(); });

const here = () => (State.inbox ? '#/eingang' : '#');
// the faces page shows the faces of where one is: those of the inbox when asked for from there
$('faces-link').addEventListener('click', (event) => { event.preventDefault(); location.hash = here() + '/gesichter'; });     // where links to categories lead: they keep the inbox
const shownMemory = () => (State.memory && State.memories ? State.memories.find((m) => m.id === State.memory) : null) || null;
async function loadMemories() {
  try { State.memories = await api('memories?areas=' + encodeURIComponent(areaQuery()), { quiet: true }); } catch { State.memories = State.memories || []; }
  renderAuto();
  if (State.memList && !$('view-library').hidden) renderMemories();
}
function memoryCard(memory) {
  return el('a', { class: 'mem-card', href: here() + '/erinnerung/' + memory.id, title: memory.text || `${memory.title} – ${memory.sub}` },
    el('img', { src: `api/photos/${memory.cover}/thumb`, loading: 'lazy', decoding: 'async', alt: '' }),
    el('span', { class: 'mem-card__words' }, el('span', { class: 'mem-card__title' }, memory.title),
      el('span', { class: 'mem-card__sub' }, t('{0} · {1} Bilder', [memory.sub, number(memory.count)]))));
}
function renderMemories() {
  const all = State.memories || [];
  const back = all.filter((m) => m.kind === 'back'); const journeys = all.filter((m) => m.kind === 'journey');
  $('grid-panel').classList.add('is-memories');
  $('memories').hidden = false;
  $('mem-tools').hidden = $('mem-text').hidden = true;
  $('btn-rename-person').hidden = $('btn-to-album').hidden = true;
  $('grid-title').textContent = t('Erinnerungen');
  showGridCount();
  $('memories').replaceChildren(
    ...(back.length ? [el('h3', {}, t('Diese Woche in früheren Jahren')), ...back.map(memoryCard)] : []),
    ...(journeys.length ? [el('h3', {}, t('Reisen')), ...journeys.map(memoryCard)] : []));
}
function showMemoryHead() {        // above the pictures of one memory: what it is, and what can be done with it
  const memory = shownMemory();
  $('grid-panel').classList.remove('is-memories');
  $('memories').hidden = true;
  $('mem-tools').hidden = !memory;
  $('mem-all').href = here() + '/erinnerungen';
  $('mem-text').hidden = !(memory && memory.text);
  if (!memory) return;
  $('mem-text').textContent = memory.text;
  $('mem-pick').textContent = State.memPicked ? t('Alle {0} Bilder zeigen', [number(memory.count)]) : t('Nur die Auswahl zeigen');
  const may = State.ai.memories && !State.me.readonly;
  $('mem-ai').hidden = !may;
  $('mem-ai').textContent = memory.by_ai ? t('KI: neuen Titel vorschlagen') : t('KI: Titel vorschlagen');
  $('mem-unai').hidden = !(may && memory.by_ai);
}
$('mem-pick').addEventListener('click', () => { State.memPicked = !State.memPicked; reload(); });
async function memoryAI(method) {
  const memory = shownMemory();
  if (!memory) return;
  for (const id of ['mem-ai', 'mem-unai']) $(id).disabled = true;
  if (method === 'POST') $('mem-ai').textContent = t('Die KI sieht sich die Bilder an …');
  try {
    const named = await api(`memories/${memory.id}/ai?areas=${encodeURIComponent(areaQuery())}`, { method });
    State.memories = State.memories.map((m) => (m.id === named.id ? named : m));
  } catch (err) {
    window.alert(err.message.includes('monthly limit') ? t('Das Monatslimit für die KI ist erreicht.') : err.message.includes('disabled') ? t('Die KI ist dafür nicht eingeschaltet.') : t('Das hat nicht geklappt: {0}', [err.message]));
  }
  for (const id of ['mem-ai', 'mem-unai']) $(id).disabled = false;
  reload(true);
}
$('mem-ai').addEventListener('click', () => memoryAI('POST'));
$('mem-unai').addEventListener('click', () => memoryAI('DELETE'));

function renderAuto() {
  const memories = State.memories || [];
  $('auto-box').hidden = !State.autos.length && !memories.length;
  $('auto').replaceChildren(...(memories.length ? [el('a', {
    class: 'tree-row' + (State.memList || State.memory ? ' is-selected' : ''), href: here() + '/erinnerungen', role: 'treeitem',
    'aria-selected': String(State.memList || Boolean(State.memory)), onclick: () => $('tree-panel').classList.remove('is-open'),
  }, el('span', { class: 'tree-name' }, t('Erinnerungen')), el('span', { class: 'tree-count' }, number(memories.length)))] : []),
  ...State.autos.map((entry) => el('a', {
    class: 'tree-row' + (entry.id === State.auto ? ' is-selected' : ''), href: here() + '/auto/' + entry.id,
    role: 'treeitem', 'aria-selected': String(entry.id === State.auto),
    onclick: () => $('tree-panel').classList.remove('is-open'),
  }, el('span', { class: 'tree-name' }, entry.name), el('span', { class: 'tree-count' }, number(entry.count)))));
}

// An album is a mark in the pictures, not a place – so it says where its pictures lie.
// Short for the list: the one folder, or the areas; in full for the heading and the tooltip.
const albumPlace = (node) => (node.folders === 1 ? node.where[0][0]
  : node.areas.length === 1 ? t('{0} · {1} Ordner', [node.areas[0], number(node.folders)]) : node.areas.join(', '));
const albumPlaces = (node) => node.where.map(([folder, count]) => `${folder} (${number(count)})`).join(', ')
  + (node.folders > node.where.length ? t(' und {0} weitere Ordner', [number(node.folders - node.where.length)]) : '');
function showGridCount() {      // how many pictures are shown – and for an album, where they lie
  if (State.memList) {
    const all = State.memories || []; const journeys = all.filter((m) => m.kind === 'journey').length; const back = all.length - journeys;
    $('grid-count').textContent = all.length ? `${journeys === 1 ? t('1 Reise') : t('{0} Reisen', [number(journeys)])}, ${back === 1 ? t('1 Rückblick') : t('{0} Rückblicke', [number(back)])}` : t('noch keine gefunden');
    return;
  }
  const total = State.total || 0;
  const shown = State.album && (State.albums || []).find((node) => node.path === State.album);
  $('grid-count').textContent = (total === 1 ? t('1 Bild') : t('{0} Bilder', [number(total)]))
    + (shown ? t(' · liegt in: {0}', [albumPlaces(shown)]) : '');
}
const albumHref = (path) => '#/album/' + path.split('/').map(encodeURIComponent).join('/');
function renderAlbums() {
  $('albums-box').hidden = !State.albums.length;
  const parents = new Set(State.albums.filter((n) => n.depth > 0).map((n) => n.path.slice(0, n.path.lastIndexOf('/'))));
  const rows = [];
  for (const node of State.albums) {
    const way = node.path.split('/');
    if (way.slice(1).some((_, i) => !State.openAlbums.has(way.slice(0, i + 1).join('/')))) continue;
    const open = State.openAlbums.has(node.path);
    const folds = parents.has(node.path);
    rows.push(el('a', {
      class: 'tree-row' + (node.path === State.album ? ' is-selected' : ''), href: albumHref(node.path),
      style: `--depth:${node.depth}`, role: 'treeitem', 'aria-selected': String(node.path === State.album),
      'aria-expanded': folds ? String(open) : null,
      oncontextmenu: (event) => {
        event.preventDefault();
        showMenu(event.clientX, event.clientY, [[t('Öffnen'), () => { location.hash = albumHref(node.path); }]]);
      },
      onclick: () => {
        if (folds && !open) { State.openAlbums.add(node.path); store.set('openAlbums', [...State.openAlbums]); }
        $('tree-panel').classList.remove('is-open');
      },
    },
    el('span', { class: 'tree-caret' + (open ? ' is-open' : '') + (folds ? '' : ' is-leaf'), 'aria-hidden': 'true', onclick: folds ? (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (open) State.openAlbums.delete(node.path); else State.openAlbums.add(node.path);
      store.set('openAlbums', [...State.openAlbums]);
      renderAlbums();
    } : null }, '›'),
    el('span', { class: 'tree-name', title: t('{0} – liegt in: {1}', [node.name, albumPlaces(node)]) }, node.name,
      el('small', { class: 'tree-where' }, albumPlace(node))),
    el('span', { class: 'tree-count' }, number(node.total))));
  }
  $('albums').replaceChildren(...rows);
  showGridCount();
}

function renderTree() {
  const tree = $('tree');
  tree.replaceChildren();
  const total = State.tree.filter((n) => n.depth === 0).reduce((sum, n) => sum + n.total, 0);
  tree.append(treeRow({ path: '', name: t('Alle Fotos'), depth: 0, total }, false));
  const parents = new Set(State.tree.filter((n) => n.depth > 0).map((n) => n.path.slice(0, n.path.lastIndexOf('/'))));
  for (const node of State.tree) {
    const parts = node.path.split('/');
    let visible = true;
    for (let i = 1; i < parts.length; i++) if (!State.open.has(parts.slice(0, i).join('/'))) { visible = false; break; }
    if (visible) tree.append(treeRow(node, parents.has(node.path)));
  }
}

function treeRow(node, hasChildren) {
  const open = State.open.has(node.path);
  const toggle = (event) => {
    event.stopPropagation();
    if (open) State.open.delete(node.path); else State.open.add(node.path);
    store.set('open', [...State.open]);
    renderTree();
  };
  const mine = node.path && State.me.write.includes(node.path.split('/')[0]);
  return el('div', {
    class: 'tree-row' + (State.folder === node.path ? ' is-selected' : ''), role: 'treeitem',
    style: `--depth:${node.path ? node.depth : 0}`, dataset: { path: node.path },
    oncontextmenu: (event) => { event.preventDefault(); treeMenu(node, event.clientX, event.clientY); },
    ondragover: mine ? (event) => { if (dragging()) { event.preventDefault(); event.currentTarget.classList.add('is-drop'); } } : null,
    ondragleave: mine ? (event) => event.currentTarget.classList.remove('is-drop') : null,
    ondrop: mine ? (event) => { event.preventDefault(); event.currentTarget.classList.remove('is-drop'); moveSelected(node.path); } : null,
    onclick: () => {
      if (hasChildren && !open) { State.open.add(node.path); store.set('open', [...State.open]); }
      location.hash = node.path ? '#/ordner/' + node.path.split('/').map(encodeURIComponent).join('/') : '#/';
      $('tree-panel').classList.remove('is-open');
    },
  },
  el('span', { class: 'tree-caret' + (open ? ' is-open' : '') + (hasChildren ? '' : ' is-leaf'), onclick: hasChildren ? toggle : null }, '›'),
  el('span', { class: 'tree-name', title: node.name }, node.name),
  el('span', { class: 'tree-count' }, number(node.total)));
}

// Right-click on a folder: make one inside it, put the chosen photos there, remove an empty one.
// One menu for every right-click. `items` are [text, action, enabled] – or null, or '-' for a line between.
function showMenu(x, y, items) {
  const menu = $('tree-menu');
  const rows = [];
  for (const entry of items) {
    if (!entry) continue;
    if (entry === '-') { if (rows.length && rows[rows.length - 1].tagName !== 'HR') rows.push(el('hr')); continue; }
    const [text, action, enabled = true] = entry;
    rows.push(el('button', { type: 'button', role: 'menuitem', disabled: !enabled,
      onclick: () => { menu.hidden = true; action(); } }, text));
  }
  while (rows.length && rows[rows.length - 1].tagName === 'HR') rows.pop();
  if (!rows.length) return;
  menu.replaceChildren(...rows);
  menu.hidden = false;
  const box = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - box.width - 4)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - box.height - 4)) + 'px';
  const first = menu.querySelector('button:not([disabled])');
  if (first) first.focus();
}
const chosenText = (n, rest) => (n === 1 ? t('1 ausgewähltes Bild {0}', [rest]) : n ? t('{0} ausgewählte Bilder {1}', [number(n), rest]) : t('Ausgewählte Bilder {0}', [rest]));

// A folder: open it, make one inside it, put the chosen photos there, remember it as an album, remove an empty one.
function treeMenu(node, x, y) {
  const n = State.selected.size;
  const mine = node.path && State.me.write.includes(node.path.split('/')[0]);
  showMenu(x, y, [
    [t('Öffnen'), () => { location.hash = node.path ? '#/ordner/' + node.path.split('/').map(encodeURIComponent).join('/') : '#/'; }],
    mine ? '-' : null,
    mine ? [t('Neuer Unterordner …'), () => newFolder(node.path)] : null,
    mine ? [chosenText(n, t('hierher verschieben')), () => moveSelected(node.path), n > 0] : null,
    mine && node.depth > 0 ? [t('Ordner als Album merken …'), () => folderToAlbum(node.path), node.total > 0] : null,
    mine && node.depth > 0 ? [t('Leeren Ordner löschen'), () => dropFolder(node.path), node.total === 0] : null,
  ]);
}

// A photo: what the bar below offers, right where the mouse is. A right-click on a photo that is not
// chosen chooses it alone – as a file manager does.
function tileMenu(button, x, y) {
  const key = button.dataset.key;
  const may = State.me.write.length > 0;
  const act = (run) => () => {
    if (!State.selected.has(key)) { State.selected.clear(); State.selected.add(key); }
    if (!State.selecting) { const kept = new Set(State.selected); setSelecting(true); for (const k of kept) State.selected.add(k); }
    markSelection();
    run();
  };
  const n = State.selected.has(key) ? State.selected.size : 1;
  const many = n > 1 ? t(' ({0} Bilder)', [number(n)]) : '';
  showMenu(x, y, [
    [t('Öffnen'), () => openViewer(key, button.closest('.month').dataset.month)],
    [t('Original laden'), () => { el('a', { href: `api/photos/${key}/file?download=true`, download: '' }).click(); }],
    may ? '-' : null,
    may ? [State.selected.has(key) ? t('Abwählen') : t('Auswählen'), () => {
      if (!State.selecting) setSelecting(true);
      if (State.selected.has(key)) State.selected.delete(key); else State.selected.add(key);
      markSelection();
    }] : null,
    may ? [t('Ändern …') + many, act(() => $('sel-edit').click())] : null,
    may ? [t('Nach links drehen') + many, act(() => turnSelected(270))] : null,
    may ? [t('Nach rechts drehen') + many, act(() => turnSelected(90))] : null,
    may && State.me.sorting ? [t('Einsortieren') + many, act(() => $('sel-sort').click())] : null,
    may ? '-' : null,
    ...(may ? State.areas.filter((a) => State.me.write.includes(a.name)).slice(0, 8)
      .map((a) => [t('Verschieben nach {0}', [a.name]) + many, act(() => moveSelected(a.name))]) : []),
    may ? '-' : null,
    may ? [t('In den Papierkorb') + many, act(() => $('sel-delete').click())] : null,
  ]);
}
$('grid').addEventListener('contextmenu', (event) => {
  const button = event.target.closest('.tile');
  if (!button) return;
  event.preventDefault();
  tileMenu(button, event.clientX, event.clientY);
});

function personMenu(person, x, y) {
  showMenu(x, y, [
    [t('Öffnen'), () => { location.hash = personHref(person.name); }],
    State.me.write.length ? [t('Umbenennen oder zusammenführen …'), () => renamePerson(person.name)] : null,
  ]);
}

document.addEventListener('click', (event) => { if (!event.target.closest('#tree-menu')) $('tree-menu').hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') $('tree-menu').hidden = true; });
window.addEventListener('blur', () => { $('tree-menu').hidden = true; });

async function newFolder(parent) {
  const name = (window.prompt(`Neuer Unterordner in ${parent}:`) || '').trim();
  if (!name) return;
  try {
    const made = await api('folders', { method: 'POST', body: JSON.stringify({ path: `${parent}/${name}` }) });
    const parts = made.path.split('/');
    for (let i = 1; i < parts.length; i++) State.open.add(parts.slice(0, i).join('/'));     // show it at once
    store.set('open', [...State.open]);
    await loadTree();
  } catch (err) { window.alert(explain(err.message)); }
}

async function dropFolder(path) {
  if (!window.confirm(t('Den leeren Ordner {0} löschen?', [path]))) return;
  try {
    await api('folders?path=' + encodeURIComponent(path), { method: 'DELETE' });
    if (State.folder === path) location.hash = '#/ordner/' + path.split('/').slice(0, -1).map(encodeURIComponent).join('/');
    await loadTree();
  } catch (err) { window.alert(explain(err.message)); }
}

// The chosen photos go into a folder – by the menu, or dragged onto the folder in the tree.
let dragged = false;
const dragging = () => dragged;
async function moveSelected(folder) {
  const n = State.selected.size;
  if (!n || !folder) return;
  if (!window.confirm(n === 1 ? t('Das Bild nach {0} verschieben?', [folder]) : t('{0} Bilder nach {1} verschieben?', [number(n), folder]))) return;
  if (!State.selecting) setSelecting(true);
  await runMany('move', { folder }, (text, isBad) => message($('sel-msg'), text, isBad), t('nach {0} verschoben', [folder]));
  State.areas = await api('areas');
  showInbox();
}
$('grid').addEventListener('dragstart', (event) => {
  const button = event.target.closest('.tile');
  if (!button || !State.selecting) { event.preventDefault(); return; }
  if (!State.selected.has(button.dataset.key)) { State.selected.add(button.dataset.key); markSelection(); }
  dragged = true;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', t('{0} Bilder', [State.selected.size]));
});
$('grid').addEventListener('dragend', () => {
  dragged = false;
  document.querySelectorAll('.tree-row.is-drop').forEach((row) => row.classList.remove('is-drop'));
});

$('btn-tree').addEventListener('click', () => $('tree-panel').classList.toggle('is-open'));

// The three parts of the side bar fold away; which ones are open is remembered per user.
function setSection(side, open) {
  side.classList.toggle('is-closed', !open);
  const head = side.querySelector('.side__head');
  head.setAttribute('aria-expanded', String(open));
  head.querySelector('.tree-caret').classList.toggle('is-open', open);
}
for (const side of document.querySelectorAll('.side')) {
  side.querySelector('.side__head').addEventListener('click', () => {
    const open = side.classList.contains('is-closed');
    setSection(side, open);
    savePrefs({ sections: { ...viewPrefs().sections, [side.dataset.section]: open } });
  });
}
const showPeopleBox = () => { $('people-box').hidden = !State.people.length && $('faces-link').hidden; };

// ----------------------------------------------------------------- filters

async function loadCameras() {
  const cameras = await api('cameras?areas=' + encodeURIComponent(areaQuery()));
  const select = $('sel-camera');
  select.replaceChildren(el('option', { value: '' }, t('Alle Kameras')));
  for (const camera of cameras) select.append(el('option', { value: camera.model }, `${camera.model} (${number(camera.count)})`));
  select.value = cameras.some((c) => c.model === State.camera) ? State.camera : '';
  State.camera = select.value;
}

// the towns are worked out from the coordinates – the list stays hidden as long as there are none
async function loadPlaceNames() {
  const countries = await api('placenames?areas=' + encodeURIComponent(areaQuery()));
  const select = $('sel-place');
  select.replaceChildren(el('option', { value: '' }, t('Alle Orte')));
  const values = new Set();
  for (const country of countries) {
    const group = el('optgroup', { label: country.country });
    group.append(el('option', { value: country.country }, t('{0} – alle ({1})', [country.country, number(country.count)])));
    values.add(country.country);
    for (const city of country.cities) {
      if (!city.city) continue;
      const value = `${country.country}|${city.city}`;
      values.add(value);
      group.append(el('option', { value }, `${city.city} (${number(city.count)})`));
    }
    select.append(group);
  }
  select.hidden = !countries.length;
  select.value = values.has(State.place) ? State.place : '';
  State.place = select.value;
}

$('sel-place').addEventListener('change', (e) => { State.place = e.target.value; reload(); });
$('sel-camera').addEventListener('change', (e) => { State.camera = e.target.value; reload(); });
$('sel-kind').addEventListener('change', (e) => { State.kind = e.target.value; reload(); });
$('chk-noplace').addEventListener('change', (e) => { State.noPlace = e.target.checked; reload(); });
$('chk-nodate').addEventListener('change', (e) => { State.noDate = e.target.checked; reload(); });
$('chk-misfit').addEventListener('change', (e) => { State.misfit = e.target.checked; reload(); });
$('chk-recursive').addEventListener('change', (e) => {
  State.recursive = e.target.checked;
  store.set('recursive', State.recursive);
  reload();
});
let searchTimer = 0;
$('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { State.q = e.target.value.trim(); reload(); }, 300);
});

// ----------------------------------------------------------------- timeline

const GAP = 4;
let observer = null;

function columns() {
  const width = $('grid').clientWidth || 800;
  const target = Math.min(State.tile, Math.max(80, (width - GAP) / 2));
  const cols = Math.max(2, Math.floor((width + GAP) / (target + GAP)));
  return { cols, tile: (width - GAP * (cols - 1)) / cols };
}

function layout() {
  const { cols, tile } = columns();
  // rows are filled, which makes them higher than asked for – so ask for a little less than the size chosen
  const high = tile * 0.8;
  $('grid').style.setProperty('--tile', `${high}px`);
  for (const grid of document.querySelectorAll('.month__grid')) {
    if (grid.dataset.filled) { grid.style.minHeight = ''; continue; }      // its pictures say how high it is
    // not loaded yet: room for about as many rows as it will need, pictures being a third wider than high on average
    const rows = Math.ceil(Number(grid.dataset.count) / Math.max(1, Math.floor(cols * 0.95)));
    grid.style.minHeight = `${rows * high * 1.15 + (rows - 1) * GAP}px`;
  }
}
window.addEventListener('resize', layout);

const monthTitle = (month) => new Date(month + '-01T12:00:00').toLocaleDateString(LOCALE, { month: 'long', year: 'numeric' });

async function reload(keepScroll = false) {
  if ((State.memList || State.memory) && State.memories === null) await loadMemories();     // opened by its address
  if (State.memList) { ++State.generation; renderMemories(); return; }
  showMemoryHead();
  const generation = ++State.generation;
  const months = await api('timeline?' + query());
  if (generation !== State.generation) return;
  const panel = $('grid-panel');
  const scroll = panel.scrollTop;
  State.months = months;
  State.items = new Map();
  if (observer) observer.disconnect();
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) loadMonth(entry.target, generation);
  }, { root: panel, rootMargin: '1200px 0px' });

  const grid = $('grid');
  grid.replaceChildren();
  for (const { month, count } of months) {
    const section = el('section', { class: 'month', dataset: { month } },
      el('h3', { class: 'month__title' }, monthTitle(month), el('small', {}, number(count)),
        el('button', { class: 'month__all', type: 'button', dataset: { all: month } }, t('alle auswählen'))),
      el('div', { class: 'month__grid', dataset: { count } }));
    grid.append(section);
    observer.observe(section);
  }
  if (State.view === 'map') loadPlaces();
  const total = months.reduce((sum, m) => sum + m.count, 0);
  $('grid-title').textContent = shownMemory() ? `${shownMemory().title} · ${shownMemory().sub}` : State.person ? State.person : State.auto ? (State.autos.find((a) => a.id === State.auto) || { name: t('Album') }).name
    : State.album ? t('Album ') + State.album.split('/').pop()
    : State.folder && !(State.inbox && !State.folder.includes('/')) ? State.folder.split('/').pop() : State.inbox ? t('Eingang') : t('Zeitleiste');
  $('btn-rename-person').hidden = !(State.person && State.me.write.length && State.people.some((p) => p.name === State.person));
  $('btn-to-album').hidden = !(State.folder.includes('/') && State.me.write.includes(State.folder.split('/')[0]));
  State.total = total;
  showGridCount();
  $('grid-empty').hidden = total > 0;
  $('grid-empty').textContent = State.scanning && !State.q ? t('Die Ordner werden gerade eingelesen …')
    : State.inbox && !State.q ? t('Der Eingang ist leer. Was das Telefon hochlädt, erscheint hier kurz danach.') : t('Hier ist nichts zu sehen.');
  layout();
  panel.scrollTop = keepScroll ? scroll : 0;
  renderYears();
}

async function loadMonth(section, generation) {
  const month = section.dataset.month;
  if (section.dataset.loaded) return;
  section.dataset.loaded = '1';
  let items;
  try { items = await api(`photos?month=${month}&${query()}`); }
  catch { delete section.dataset.loaded; return; }
  if (generation !== State.generation) return;
  State.items.set(month, items);
  const grid = section.querySelector('.month__grid');
  const fragment = document.createDocumentFragment();
  for (const item of items) fragment.append(tile(item));
  grid.replaceChildren(fragment);
  grid.dataset.filled = '1';
  grid.style.minHeight = '';
}

// how much wider than high a picture is shown: its own shape, but a panorama does not take the whole row
const shape = (item) => `--r:${item.w > 0 && item.h > 0 ? Math.min(3.5, Math.max(0.5, item.w / item.h)).toFixed(3) : 1}`;

function tile(item) {
  const label = new Date(item.taken).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' });
  if (item.kind === 'video') {
    // the picture out of the film is there only if the server could make one; without it the tile says what it is
    const still = el('img', { src: `api/photos/${item.key}/thumb`, loading: 'lazy', decoding: 'async', alt: '', draggable: 'false' });
    still.addEventListener('error', () => still.replaceWith('▶'));
    return el('button', { class: 'tile tile--video' + (State.selected.has(item.key) ? ' is-selected' : ''), type: 'button', draggable: 'true', dataset: { key: item.key }, style: shape(item), title: label, 'aria-label': t('Video vom ') + label },
      still, el('span', { class: 'tile__badge' }, item.s ? '▶ ' + runtime(item.s) : t('Video')));
  }
  return el('button', { class: 'tile' + (State.selected.has(item.key) ? ' is-selected' : ''), type: 'button', draggable: 'true', dataset: { key: item.key },
    style: shape(item), title: label, 'aria-label': t('Foto vom {0}', [label]), 'aria-pressed': State.selecting ? String(State.selected.has(item.key)) : null },
    el('img', { src: thumbUrl(item), loading: 'lazy', decoding: 'async', alt: '', draggable: 'false' }));
}

const runtime = (seconds) => {
  const all = Math.max(1, Math.round(seconds));
  const h = Math.floor(all / 3600); const m = Math.floor(all % 3600 / 60); const s = String(all % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};

// The address names what the picture depends on, so a turned picture is never served from the browser's memory.
const thumbUrl = (item) => `api/photos/${item.key}/thumb?o=${item.o || 1}`;
const previewUrl = (photo) => `api/photos/${photo.key}/preview?o=${photo.orientation || 1}`;

$('grid').addEventListener('click', async (event) => {
  const all = event.target.closest('.month__all');
  if (all) {
    for (const item of await monthItems(all.dataset.all)) State.selected.add(item.key);
    markSelection();
    return;
  }
  const button = event.target.closest('.tile');
  if (!button) return;
  if (State.selecting) toggleSelected(button, event.shiftKey);
  else openViewer(button.dataset.key, button.closest('.month').dataset.month);
});

// ----------------------------------------------------------------- sizes
// How big things are is a matter of the screen in front of you, so it is remembered in this browser.

// … and per user and folder: holiday pictures small, scanned letters big.
const viewKey = () => 'view.' + (State.me ? State.me.name : '');
const viewPrefs = () => ({ tiles: {}, tree: null, sections: {}, ...store.get(viewKey(), {}) });
function savePrefs(change) { store.set(viewKey(), { ...viewPrefs(), ...change }); }

function tileFor(folder) {       // the folder's own size, else the nearest parent's, else the usual one
  const { tiles } = viewPrefs();
  for (let at = folder; ; at = at.includes('/') ? at.slice(0, at.lastIndexOf('/')) : '') {
    if (tiles[at]) return tiles[at];
    if (!at) return window.innerWidth < 600 ? 110 : 150;
  }
}

function applyView() {
  State.tile = tileFor(State.folder);
  $('tile-size').value = State.tile;
  const { tree, sections } = viewPrefs();
  const face = viewPrefs().faces || 88;
  $('face-size').value = face; sizeFaces(face);
  faceColumn('faceList', viewPrefs().faceList); faceColumn('faceRef', viewPrefs().faceRef);
  $('tree-panel').style.width = tree && window.innerWidth > 800 ? tree + 'px' : '';
  for (const side of document.querySelectorAll('.side')) setSection(side, sections[side.dataset.section] !== false);
}

$('tile-size').addEventListener('input', (event) => {
  State.tile = Number(event.target.value);
  savePrefs({ tiles: { ...viewPrefs().tiles, [State.folder]: State.tile } });
  layout();
});

const TREE = { min: 160, fallback: 0 };
function treeWidth(px) {
  const panel = $('tree-panel');
  if (!px) { panel.style.width = ''; savePrefs({ tree: null }); return; }
  const width = Math.round(Math.max(TREE.min, Math.min(px, window.innerWidth * 0.6)));
  panel.style.width = width + 'px';
  savePrefs({ tree: width });
}
{
  const grip = $('tree-grip');
  grip.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    grip.setPointerCapture(event.pointerId);
    grip.classList.add('is-dragging');
    const left = $('tree-panel').getBoundingClientRect().left;
    const move = (e) => { treeWidth(e.clientX - left); layout(); };
    const stop = () => {
      grip.classList.remove('is-dragging');
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', stop);
      grip.removeEventListener('pointercancel', stop);
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', stop);
    grip.addEventListener('pointercancel', stop);
  });
  grip.addEventListener('dblclick', () => { treeWidth(0); layout(); });
  grip.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    treeWidth($('tree-panel').getBoundingClientRect().width + (event.key === 'ArrowRight' ? 20 : -20));
    layout();
  });
}

// ----------------------------------------------------------------- map of the photos

const Places = { map: null, layer: null, points: [], fitted: '' };
const CELL = 76;      // px: photos closer together than this share one picture on the map

function setView(view) {
  State.view = view;
  const map = view === 'map';
  $('grid').hidden = map;
  $('places').hidden = !map;
  for (const [id, on] of [['view-grid', !map], ['view-map', map]]) {
    $(id).classList.toggle('is-on', on);
    $(id).setAttribute('aria-pressed', String(on));
  }
  reload(true);
}
$('view-grid').addEventListener('click', () => setView('grid'));
$('view-map').addEventListener('click', () => setView('map'));

async function loadPlaces() {
  if (!Places.map) {
    L.Icon.Default.imagePath = 'vendor/leaflet/images/';
    Places.map = L.map('places', { worldCopyJump: true }).setView([51.2, 10.4], 6);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
    }).addTo(Places.map);
    Places.layer = L.layerGroup().addTo(Places.map);
    Places.map.on('moveend zoomend', drawPlaces);
  }
  Places.map.invalidateSize();
  const asked = query();
  let points;
  try { points = await api('places?' + asked); } catch { return; }
  if (asked !== query() || State.view !== 'map') return;
  Places.points = points;
  const all = State.months.reduce((sum, m) => sum + m.count, 0);
  $('grid-empty').hidden = points.length > 0;
  $('grid-empty').textContent = t('In dieser Auswahl hat kein Bild einen Ort.');
  $('grid-count').textContent = t('{0} von {1} Bildern haben einen Ort', [number(points.length), number(all)]);
  if (points.length && Places.fitted !== asked) {     // a new selection: show all of it
    Places.fitted = asked;
    Places.map.fitBounds(L.latLngBounds(points.map((p) => [p[1], p[2]])).pad(0.1), { maxZoom: 15 });
  }
  drawPlaces();
}

function drawPlaces() {
  const map = Places.map;
  if (!map || State.view !== 'map') return;
  const zoom = map.getZoom();
  const seen = map.getBounds().pad(0.25);
  const groups = new Map();
  for (const point of Places.points) {
    if (!seen.contains([point[1], point[2]])) continue;
    const at = map.project([point[1], point[2]], zoom);
    const id = Math.floor(at.x / CELL) + ':' + Math.floor(at.y / CELL);
    let group = groups.get(id);
    if (!group) groups.set(id, group = { items: [], lat: 0, lon: 0 });
    group.items.push(point);
    group.lat += point[1];
    group.lon += point[2];
  }
  Places.layer.clearLayers();
  for (const group of groups.values()) {
    const n = group.items.length;
    const first = group.items[0];
    const icon = L.divIcon({ className: 'place', iconSize: [60, 60],
      html: `<img src="${thumbUrl({ key: first[0], o: first[4] })}" alt="">${n > 1 ? `<span>${number(n)}</span>` : ''}` });
    L.marker([group.lat / n, group.lon / n], { icon, keyboard: false, title: n === 1 ? t('1 Bild') : t('{0} Bilder', [number(n)]) })
      .on('click', () => openPlace(group)).addTo(Places.layer);
  }
}

function openPlace(group) {
  const { items } = group;
  if (items.length === 1) { openViewer(items[0][0], items[0][3]); return; }
  const bounds = L.latLngBounds(items.map((p) => [p[1], p[2]]));
  const tight = bounds.getNorthEast().distanceTo(bounds.getSouthWest()) < 15;      // metres
  if (!tight && Places.map.getZoom() < 19) { Places.map.fitBounds(bounds.pad(0.3), { maxZoom: 19 }); return; }
  const strip = el('div', { class: 'place-strip' }, ...items.slice(0, 80).map((p) => el('button', { type: 'button',
    'aria-label': t('Bild öffnen'), onclick: () => openViewer(p[0], p[3]) },
    el('img', { src: thumbUrl({ key: p[0], o: p[4] }), loading: 'lazy', alt: '' }))));
  const box = el('div', {}, el('p', { class: 'field-hint' }, items.length > 80 ? t('{0} Bilder, die ersten 80:', [number(items.length)]) : t('{0} Bilder an dieser Stelle', [number(items.length)])), strip);
  L.popup({ maxWidth: 300 }).setLatLng([group.lat / items.length, group.lon / items.length]).setContent(box).openOn(Places.map);
}

// ----------------------------------------------------------------- years

function renderYears() {
  const nav = $('years');
  nav.replaceChildren();
  const years = [...new Set(State.months.map((m) => m.month.slice(0, 4)))];
  nav.hidden = years.length < 2 || State.view === 'map';
  for (const year of years) {
    nav.append(el('button', { type: 'button', dataset: { year }, onclick: () => {
      const section = document.querySelector(`.month[data-month^="${year}"]`);
      if (section) $('grid-panel').scrollTop = section.offsetTop - 4;
    } }, year));
  }
  markYear();
}

function markYear() {
  const panel = $('grid-panel');
  const top = panel.scrollTop + 60;
  let current = null;
  for (const section of document.querySelectorAll('.month')) {
    if (section.offsetTop > top) break;
    current = section.dataset.month.slice(0, 4);
  }
  current = current || (State.months[0] && State.months[0].month.slice(0, 4));
  for (const button of $('years').children) {
    const hit = button.dataset.year === current;
    button.classList.toggle('is-current', hit);
    if (hit) button.scrollIntoView({ block: 'nearest' });
  }
}
let yearTick = 0;
$('grid-panel').addEventListener('scroll', () => {
  cancelAnimationFrame(yearTick);
  yearTick = requestAnimationFrame(markYear);
}, { passive: true });

// ----------------------------------------------------------------- viewer

const Viewer = { key: null, month: null, photo: null, faces: [] };

async function monthItems(month) {
  if (!State.items.has(month)) State.items.set(month, await api(`photos?month=${month}&${query()}`));
  return State.items.get(month);
}

async function neighbour(step) {
  if (!Viewer.month) return null;       // opened from a page that has no order of its own
  const items = await monthItems(Viewer.month);
  const at = items.findIndex((i) => i.key === Viewer.key) + step;
  if (at >= 0 && at < items.length) return { key: items[at].key, month: Viewer.month };
  const m = State.months.findIndex((x) => x.month === Viewer.month) + step;
  if (m < 0 || m >= State.months.length) return null;
  const next = await monthItems(State.months[m].month);
  if (!next.length) return null;
  return { key: next[step > 0 ? 0 : next.length - 1].key, month: State.months[m].month };
}

function fact(list, label, ...content) {
  list.append(el('dt', {}, label), el('dd', {}, ...content));
}

const DATE_SOURCE = {
  exif: '', name: t('aus dem Dateinamen, im Bild steht keins'), file: t('Dateidatum, im Bild steht keins'),
};

async function openViewer(key, month) {
  Viewer.key = key;
  Viewer.month = month;
  $('viewer').hidden = false;
  const img = $('v-img'); const video = $('v-video');
  video.pause();
  img.hidden = video.hidden = $('v-broken').hidden = true;
  let photo;
  try { photo = await api('photos/' + key); } catch { closeViewer(); return; }
  if (Viewer.key !== key) return;
  message($('v-msg'), '');
  showPhoto(photo);
}

function showPhoto(photo) {
  const key = photo.key;
  const img = $('v-img'); const video = $('v-video');
  Viewer.photo = photo;
  img.hidden = video.hidden = $('v-broken').hidden = true;
  $('v-tools').hidden = !photo.can_delete;
  $('v-edit').hidden = !photo.can_write;
  $('v-develop').hidden = !photo.can_develop;
  $('v-develop').textContent = photo.original ? t('Bild weiter bearbeiten') : t('Bild bearbeiten');
  devClose();
  $('v-ai-box').hidden = !(State.ai.enabled && photo.can_write && !photo.error && !State.ai.excluded_areas.includes(photo.area));
  showTake(photo);
  $('v-left').hidden = $('v-right').hidden = !photo.can_turn;
  $('v-form').hidden = true;
  $('v-facts').hidden = false;
  Viewer.faces = [];
  $('v-faces').hidden = $('v-people').hidden = true;

  $('v-live').hidden = !(photo.film && !photo.error);
  $('v-live').textContent = t('▶ Live');
  if (photo.kind === 'video') {
    video.src = `api/photos/${key}/file`;
    video.hidden = false;
  } else if (photo.error) {
    $('v-broken').hidden = false;
  } else {
    img.src = previewUrl(photo);
    img.hidden = false;
  }
  $('v-name').textContent = photo.name;
  $('v-download').href = `api/photos/${key}/file?download=true`;

  const facts = $('v-facts');
  facts.replaceChildren();
  const when = new Date(photo.taken).toLocaleString(LOCALE, { weekday: 'short', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  fact(facts, t('Datum'), when, DATE_SOURCE[photo.taken_src] ? el('span', { class: 'hint' }, DATE_SOURCE[photo.taken_src]) : null);
  fact(facts, t('Ordner'), el('a', { href: '#/ordner/' + photo.folder.split('/').map(encodeURIComponent).join('/'), onclick: closeViewer }, photo.folder));
  if (photo.description) fact(facts, t('Beschreibung'), photo.description);
  if (photo.keywords.length) fact(facts, t('Schlagworte'), ...photo.keywords.map((k) => el('span', { class: 'tag' }, k)));
  if (photo.albums.length) fact(facts, t('Alben'), ...photo.albums.map((a) => el('a', { class: 'tag', href: albumHref(a), onclick: closeViewer }, a)));
  facts.append(el('dt', { id: 'v-people-label', hidden: true }, t('Personen')), el('dd', { id: 'v-people-fact', hidden: true }));
  if (photo.kind === 'image' && !photo.error) loadFaces(photo);
  if (photo.misfit) {
    fact(facts, t('Ablage'), t('gehört nach {0}', [photo.target]),
      photo.can_delete ? el('button', { class: 'fd-button fd-button--compact', type: 'button', onclick: () => sortShown() }, t('Einsortieren')) : null);
  }
  if (photo.rating > 0) fact(facts, t('Bewertung'), '★'.repeat(photo.rating) + '☆'.repeat(5 - photo.rating));
  if (photo.lat !== null && photo.lon !== null) {
    const named = [photo.city, photo.state, photo.country].filter(Boolean).join(', ');
    fact(facts, t('Ort'), named ? el('div', { title: t('nächster Ort zu den Koordinaten im Bild') }, named) : null,
      el('a', { href: `https://www.openstreetmap.org/?mlat=${photo.lat}&mlon=${photo.lon}#map=15/${photo.lat}/${photo.lon}`, target: '_blank', rel: 'noopener noreferrer' },
        `${photo.lat.toFixed(5)}, ${photo.lon.toFixed(5)}`), el('span', { class: 'hint' }, t('öffnet OpenStreetMap')));
  } else {
    // the place is set in the edit form – say so right here, and open the map at once
    fact(facts, t('Ort'), el('span', { class: 'hint' }, t('kein Ort im Bild')), photo.can_write ? el('button', {
      class: 'fd-button fd-button--compact', type: 'button', onclick: () => { $('v-edit').click(); $('e-map').click(); },
    }, t('Ort wählen')) : null);
  }
  const camera = [photo.make, photo.model].filter(Boolean).join(' ');
  if (camera) fact(facts, t('Kamera'), photo.model && photo.model.startsWith(photo.make) ? photo.model : camera);
  const size = photo.size > 1048576 ? `${(photo.size / 1048576).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} MB` : `${Math.round(photo.size / 1024)} KB`;
  fact(facts, t('Datei'), photo.width ? `${photo.width} × ${photo.height}, ${size}` : size);
  if (photo.kind === 'video' && photo.seconds) fact(facts, t('Länge'), runtime(photo.seconds));
  if (photo.original) {
    fact(facts, t('Bearbeitet'), devWords(photo.recipe), el('a', { href: `api/photos/${photo.original}/file`, target: '_blank', rel: 'noopener' }, t('Original ansehen')));
  }
  if (photo.film) fact(facts, t('Live Photo'), t('mit kurzem Film'), el('span', { class: 'hint' }, t('der Film liegt neben dem Foto und wandert mit ihm')));
  if (photo.orientation > 1) fact(facts, t('Ausrichtung'), t('per Markierung gedreht'), el('span', { class: 'hint' }, t('die Bilddaten selbst sind unverändert')));
  if (photo.error) fact(facts, t('Fehler'), photo.error);

  neighbour(-1).then((n) => { if (Viewer.key === key) $('v-prev').disabled = !n; });
  neighbour(1).then((n) => {
    if (Viewer.key !== key) return;
    $('v-next').disabled = !n;
    if (n) api('photos/' + n.key, { quiet: true }).then((p) => { if (p.kind === 'image') new Image().src = previewUrl(p); }).catch(() => {});
  });
}

// ----------------------------------------------------------------- working on the picture itself
// Cut, set straight, light and colours. The server works the picture out, here it is only shown:
// the preview comes back set straight and coloured but not cut, and the cut is drawn over it.

const DEV_SLIDERS = ['angle', 'brightness', 'contrast', 'saturation', 'warmth'];
const DEV_NAMES = { angle: t('gerade gerückt'), crop: t('zugeschnitten'), brightness: t('Helligkeit'), contrast: t('Kontrast'), saturation: t('Sättigung'), warmth: t('Wärme'), auto: t('Licht ausgeglichen') };
const Dev = { on: false, crop: [0, 0, 1, 1], timer: null, run: 0, url: null };
const devWords = (recipe) => Object.keys(recipe || {}).map((k) => DEV_NAMES[k]).filter(Boolean).join(', ') || t('ohne Änderung');

function devRecipe(withCrop) {
  const recipe = {};
  for (const name of DEV_SLIDERS) { const value = Number($('d-' + name).value); if (value) recipe[name] = value; }
  if ($('d-auto').checked) recipe.auto = true;
  const [x, y, w, h] = Dev.crop;
  if (withCrop && (x > 0.001 || y > 0.001 || w < 0.999 || h < 0.999)) recipe.crop = [x, y, Math.min(w, 1 - x), Math.min(h, 1 - y)];
  return recipe;
}

function devLabels() {
  for (const name of DEV_SLIDERS) {
    const value = Number($('d-' + name).value);
    $(`d-${name}-out`).textContent = name === 'angle' ? `${value.toLocaleString(LOCALE, { maximumFractionDigits: 1 })}°` : (value > 0 ? '+' : '') + value;
  }
}

function devPlace() {          // the cut lies over the picture as it is shown, whatever the window does
  const img = $('v-img'); const stage = img.parentElement.getBoundingClientRect(); const box = img.getBoundingClientRect();
  Object.assign($('v-crop').style, { left: `${box.left - stage.left}px`, top: `${box.top - stage.top}px`, width: `${box.width}px`, height: `${box.height}px` });
  const [x, y, w, h] = Dev.crop;
  Object.assign($('v-crop-box').style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
}

function devShow() {           // ask for the picture as it would look; only the latest answer counts
  clearTimeout(Dev.timer);
  Dev.timer = setTimeout(async () => {
    const run = ++Dev.run; const key = Viewer.key;
    try {
      const res = await fetch(`api/photos/${key}/develop/preview`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(devRecipe(false)),
      });
      if (!res.ok || run !== Dev.run || !Dev.on || Viewer.key !== key) return;
      const url = URL.createObjectURL(await res.blob());
      $('v-img').src = url;
      if (Dev.url) URL.revokeObjectURL(Dev.url);
      Dev.url = url;
    } catch { /* the last picture stays */ }
  }, 120);
}

function devRatio() {          // width by height of the cut, in the picture's own measure – 0 for free
  const choice = $('d-ratio').value; const img = $('v-img');
  if (!choice || !img.naturalWidth) return 0;
  const high = img.naturalHeight > img.naturalWidth;
  if (choice === 'orig') return 1;
  const [a, b] = choice.includes(':') ? choice.split(':').map(Number) : [1, 1];
  const wanted = high ? b / a : a / b;                       // an upright picture gets the upright shape
  return wanted / (img.naturalWidth / img.naturalHeight);    // as width share by height share
}

function devFit() {            // bring the cut into the chosen shape, as large as fits, around its middle
  const ratio = devRatio();
  if (!ratio) return;
  let [x, y, w, h] = Dev.crop;
  const cx = x + w / 2; const cy = y + h / 2;
  w = Math.min(1, ratio); h = w / ratio;
  if (h > 1) { h = 1; w = ratio; }
  const scale = Math.min(1, Math.max(Dev.crop[2] / w, Dev.crop[3] / h));
  w *= scale; h *= scale;
  Dev.crop = [Math.min(1 - w, Math.max(0, cx - w / 2)), Math.min(1 - h, Math.max(0, cy - h / 2)), w, h];
}

function devOpen() {
  const photo = Viewer.photo;
  if (!photo || !photo.can_develop) return;
  liveBack();
  const recipe = photo.original ? (photo.recipe || {}) : {};
  for (const name of DEV_SLIDERS) $('d-' + name).value = recipe[name] || 0;
  $('d-auto').checked = Boolean(recipe.auto);
  $('d-ratio').value = '';
  Dev.crop = recipe.crop ? [...recipe.crop] : [0, 0, 1, 1];
  Dev.on = true;
  devLabels();
  $('d-discard').hidden = !photo.original;
  $('v-facts').hidden = $('v-form').hidden = $('v-faces').hidden = true;
  $('v-people').hidden = true;
  $('v-dev').hidden = $('v-crop').hidden = false;
  $('v-prev').hidden = $('v-next').hidden = true;
  message($('v-msg'), '');
  devPlace();
  devShow();
}

function devClose() {
  if (!Dev.on) return;
  Dev.on = false;
  Dev.run++;
  clearTimeout(Dev.timer);
  $('v-dev').hidden = $('v-crop').hidden = true;
  $('v-prev').hidden = $('v-next').hidden = false;
  if (Dev.url) { URL.revokeObjectURL(Dev.url); Dev.url = null; }
}

function devLeave() {          // back to the picture as it is
  devClose();
  if (Viewer.photo) showPhoto(Viewer.photo);
}

$('v-develop').addEventListener('click', devOpen);
$('d-cancel').addEventListener('click', devLeave);
$('d-reset').addEventListener('click', () => {
  for (const name of DEV_SLIDERS) $('d-' + name).value = 0;
  $('d-auto').checked = false; $('d-ratio').value = '';
  Dev.crop = [0, 0, 1, 1];
  devLabels(); devPlace(); devShow();
});
for (const name of DEV_SLIDERS) $('d-' + name).addEventListener('input', () => { devLabels(); devShow(); });
$('d-auto').addEventListener('change', devShow);
$('d-ratio').addEventListener('change', () => { devFit(); devPlace(); });
$('v-img').addEventListener('load', () => { if (Dev.on) devPlace(); });
window.addEventListener('resize', () => { if (Dev.on) devPlace(); });

$('v-crop').addEventListener('pointerdown', (event) => {
  const grip = event.target.dataset.grip; const inside = event.target.closest('#v-crop-box');
  if (!grip && !inside) return;
  event.preventDefault();
  const area = $('v-crop').getBoundingClientRect(); const start = [...Dev.crop]; const from = [event.clientX, event.clientY];
  const ratio = devRatio(); const MIN = 0.05;
  const move = (e) => {
    const dx = (e.clientX - from[0]) / area.width; const dy = (e.clientY - from[1]) / area.height;
    let [x, y, w, h] = start;
    if (!grip) {
      x = Math.min(1 - w, Math.max(0, x + dx)); y = Math.min(1 - h, Math.max(0, y + dy));
    } else {
      let left = x; let top = y; let right = x + w; let bottom = y + h;       // the corner opposite stays where it is
      if (grip.includes('w')) left = Math.min(right - MIN, Math.max(0, left + dx)); else right = Math.max(left + MIN, Math.min(1, right + dx));
      if (grip.includes('n')) top = Math.min(bottom - MIN, Math.max(0, top + dy)); else bottom = Math.max(top + MIN, Math.min(1, bottom + dy));
      if (ratio) {
        let wide = right - left; let high = wide / ratio;
        const room = grip.includes('n') ? bottom : 1 - top;
        if (high > room) { high = room; wide = high * ratio; }
        if (grip.includes('w')) left = right - wide; else right = left + wide;
        if (grip.includes('n')) top = bottom - high; else bottom = top + high;
      }
      x = left; y = top; w = right - left; h = bottom - top;
    }
    Dev.crop = [x, y, w, h];
    devPlace();
  };
  const stop = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop);
});

$('v-dev').addEventListener('submit', async (event) => {
  event.preventDefault();
  const recipe = devRecipe(true);
  if (!Object.keys(recipe).length) { message($('v-msg'), t('Es ist nichts verändert.'), true); return; }
  const button = event.submitter; if (button) button.disabled = true;
  message($('v-msg'), t('Das Bild wird gerechnet …'));
  try {
    const photo = await api(`photos/${Viewer.key}/develop`, { method: 'POST', body: JSON.stringify(recipe) });
    devClose();
    Viewer.key = photo.key; State.dirty = true;
    showPhoto(photo);
    message($('v-msg'), t('Gespeichert. Das Original liegt dahinter.'));
  } catch (err) { message($('v-msg'), explain(err.message), true); }
  if (button) button.disabled = false;
});

$('d-discard').addEventListener('click', async () => {
  if (!window.confirm(t('Die bearbeitete Fassung in den Papierkorb legen und wieder das Original zeigen?'))) return;
  try {
    const photo = await api(`photos/${Viewer.key}/develop`, { method: 'DELETE' });
    devClose();
    Viewer.key = photo.key; State.dirty = true;
    showPhoto(photo);
    message($('v-msg'), t('Das Original ist wieder da. Die bearbeitete Fassung liegt im Papierkorb.'));
  } catch (err) { message($('v-msg'), explain(err.message), true); }
});

// A Live Photo: its short film plays in place of the picture and makes way for it again.
function liveBack() {
  const photo = Viewer.photo; const video = $('v-video');
  if (!photo || photo.kind !== 'image' || video.hidden) return;
  video.pause();
  video.removeAttribute('src');
  video.hidden = true;
  $('v-img').hidden = false;
  $('v-faces').hidden = !Viewer.faces.length;
  $('v-live').textContent = t('▶ Live');
}

$('v-live').addEventListener('click', () => {
  const photo = Viewer.photo; const video = $('v-video');
  if (!photo || !photo.film) return;
  if (!video.hidden) { liveBack(); return; }
  video.src = `api/photos/${photo.film}/file`;
  $('v-img').hidden = $('v-faces').hidden = true;
  video.hidden = false;
  $('v-live').textContent = t('■ Foto');
  video.play().catch(() => {});
});
$('v-video').addEventListener('ended', liveBack);
$('v-video').addEventListener('error', () => {
  if (Viewer.photo && Viewer.photo.kind === 'image' && !$('v-video').hidden) {
    liveBack();
    message($('v-msg'), t('Dieser Browser kann den Film nicht abspielen.'), true);
  }
});

async function step(direction) {
  const next = await neighbour(direction);
  if (next) openViewer(next.key, next.month);
}

function closeViewer() {
  $('v-video').pause();
  $('v-video').removeAttribute('src');
  $('viewer').hidden = true;
  const button = document.querySelector(`.tile[data-key="${Viewer.key}"]`);
  Viewer.key = null;
  if (button) button.focus({ preventScroll: false });
  if (!$('view-faces').hidden) { if (State.dirty) { State.stale = true; renderFaceGroups(); } return; }
  if (State.dirty) { State.dirty = false; loadTree(); reload(true); }
}

$('v-img').addEventListener('error', () => { $('v-img').hidden = true; $('v-broken').hidden = false; });
$('v-prev').addEventListener('click', () => step(-1));
$('v-next').addEventListener('click', () => step(1));
$('v-close').addEventListener('click', closeViewer);
document.addEventListener('keydown', (event) => {
  if ($('viewer').hidden || ['VIDEO', 'INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
  if (document.querySelector('dialog[open]')) return;
  if (Dev.on) { if (event.key === 'Escape') devLeave(); return; }     // working on the picture: no leafing on
  if (event.key === 'Escape') closeViewer();
  else if (event.key === 'ArrowLeft') step(-1);
  else if (event.key === 'ArrowRight') step(1);
  else if (event.key === 'Delete' && Viewer.photo && Viewer.photo.can_delete) deleteShown();
});

// ----------------------------------------------------------------- routing

function route() {
  const hash = decodeURI(location.hash.replace(/^#\/?/, ''));
  const settings = hash.startsWith('einstellungen');
  const bin = hash.startsWith('papierkorb');
  const facesIn = hash.startsWith('eingang/gesichter');       // the faces found in what the phone sent
  const faces = hash.startsWith('gesichter') || facesIn;
  const copies = hash.startsWith('duplikate');
  $('view-dupes').hidden = !copies;
  $('view-settings').hidden = !settings;
  $('view-trash').hidden = !bin;
  $('view-faces').hidden = !faces;
  $('view-library').hidden = settings || bin || faces || copies;
  if (facesIn && !State.inbox) { State.inbox = true; State.stale = true; showInbox(); }
  if ((settings || bin || faces || copies) && State.inbox && !facesIn) {     // the other pages are about the collection
    State.inbox = false;
    State.stale = true;
    showInbox();
  }
  if (settings) { showSettings(hash.split('/')[1]); return false; }
  if (bin) { renderTrash(); return false; }
  if (faces) { renderFaceGroups(); return false; }
  if (copies) { renderDupes(); return false; }
  // a category, a memory or a person looked at from inside the inbox stays there: eingang/auto/live
  const within = hash.startsWith('eingang/');
  const rest = within ? hash.slice(8) : hash;
  const folder = hash.startsWith('ordner/') ? hash.slice(7).split('/').map(decodeURIComponent).join('/') : '';
  const album = hash.startsWith('album/') ? hash.slice(6).split('/').map(decodeURIComponent).join('/') : '';
  const person = rest.startsWith('person/') ? decodeURIComponent(rest.slice(7)) : '';
  const inbox = hash === 'eingang' || within || inInbox(folder);       // a folder inside the inbox is still the inbox
  const auto = rest.startsWith('auto/') ? rest.slice(5) : '';
  const memory = rest.startsWith('erinnerung/') ? rest.slice(11) : '';
  const memList = rest === 'erinnerungen';
  const changed = folder !== State.folder || album !== State.album || person !== State.person || inbox !== State.inbox || auto !== State.auto
    || memory !== State.memory || memList !== State.memList;
  if (memory !== State.memory) State.memPicked = true;
  State.auto = auto;
  State.memory = memory;
  State.memList = memList;
  if (inbox !== State.inbox && State.selecting) setSelecting(false);
  State.inbox = inbox;
  State.folder = folder;
  if (album && album !== State.album) {       // make the chosen album visible in its tree
    const parts = album.split('/');
    for (let i = 1; i < parts.length; i++) State.openAlbums.add(parts.slice(0, i).join('/'));
  }
  State.album = album;
  State.person = person;
  return changed;
}

window.addEventListener('hashchange', () => {
  const changed = route();
  if (!State.me || !$('view-settings').hidden || !$('view-trash').hidden || !$('view-faces').hidden || !$('view-dupes').hidden) return;
  if (State.folder) {   // make the chosen folder visible in the tree
    const parts = State.folder.split('/');
    for (let i = 1; i < parts.length; i++) State.open.add(parts.slice(0, i).join('/'));
  }
  renderChips();
  renderTree();
  renderAlbums();
  renderAuto();
  renderPeople();
  if (changed || State.stale) { State.stale = false; applyView(); loadTree(); loadCameras(); loadPlaceNames(); reload(); } else layout();
});

// ----------------------------------------------------------------- settings

const PASSWORD_ERRORS = { 'wrong password': t('Das bisherige Passwort stimmt nicht.') };

function message(node, text, bad = false) {
  node.textContent = text;
  node.classList.toggle('is-bad', bad);
}

async function showSettings(wanted) {
  if (!State.me) return;
  const panes = [...document.querySelectorAll('.set-link')].filter((a) => State.me.is_admin || !a.classList.contains('admin-only')).map((a) => a.dataset.pane);
  const pane = panes.includes(wanted) ? wanted : (document.querySelector('.set-link.is-active') || {}).dataset?.pane || 'konto';
  document.querySelectorAll('.set-link').forEach((a) => a.classList.toggle('is-active', a.dataset.pane === pane));
  document.querySelectorAll('.set-pane').forEach((p) => { p.hidden = p.dataset.pane !== pane; });
  $('trash-days').disabled = !State.me.is_admin;
  api('trash').then((bin) => { $('trash-days').value = bin.days; }).catch(() => {});
  $('me-line').textContent = t('Angemeldet als {0} ({1})', [State.me.display_name, State.me.name]) + (State.me.is_admin ? t(', Administrator.') : '.');
  try {
    const status = await api('status');
    $('sys-support').hidden = !status.support;
    if (status.support) $('sys-support-link').href = status.support;
    $('sys-line').textContent = t('Version {0} · {1} Bilder im Index', [status.version, number(status.photos)])
      + (status.scan.running ? t(' · liest gerade ein ({0} von {1})', [number(status.scan.done), number(status.scan.total)]) : '')
      + (status.readonly ? t(' · Fotos nur lesend eingebunden') : '');
  } catch { /* leave empty */ }
  if (pane !== 'benutzer') $('usr-form').hidden = true;
  if (pane === 'konto') loadAccountSecurity();
  if (pane === 'benutzer') { loadUsers(); renderAreaRows(); }
  if (pane === 'sicherheit') loadSecurity();
  if (pane === 'api') { loadApiKey(); loadUpload(); }
  if (pane === 'ki') loadAI();
  if (pane === 'personen') loadFaceSettings();
  if (pane === 'ordnen') loadSorting();
  clearTimeout(jobsTimer);
  if (pane === 'auftraege') { message($('job-msg'), ''); showJobs(); }
  if (pane === 'aenderungen') renderJournal();
}

$('pw-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const msg = $('pw-msg');
  if ($('pw-new').value !== $('pw-new2').value) { message(msg, t('Die beiden neuen Passwörter sind verschieden.'), true); return; }
  try {
    await api('me/password', { method: 'POST', body: JSON.stringify({ old: $('pw-old').value, new: $('pw-new').value }) });
    event.target.reset();
    message(msg, t('Passwort geändert.'));
  } catch (err) {
    message(msg, PASSWORD_ERRORS[err.message] || (err.message.includes('too short') ? t('Mindestens 12 Zeichen.') : err.message), true);
  }
});

// ----------------------------------------------------------------- users (as in Ordnung!)

const PERMS = { none: t('kein Zugriff'), read: t('lesen'), write: t('lesen + schreiben') };
const areaColor = (name) => AREA_COLORS[Math.max(0, State.areas.findIndex((a) => a.name === name)) % AREA_COLORS.length];
const clock = (ts) => new Date(ts * 1000).toLocaleString(LOCALE, { dateStyle: 'short', timeStyle: 'short' });
const cell = (...children) => el('td', { class: 'fd-table__cell' }, ...children);
let editing = null;     // null = new user, else the name

async function loadUsers() {
  const users = await api('users');
  $('usr-rows').replaceChildren(...users.map((u) => {
    const areas = u.is_admin ? t('alle')
      : (Object.entries(u.areas).map(([a, p]) => `${a} (${PERMS[p]})`).join(', ') || t('keine'));
    const actions = el('td', { class: 'fd-table__cell usr-actions' },
      el('button', { class: 'fd-button fd-button--transparent', type: 'button', onclick: () => editUser(u) }, t('Bearbeiten')),
      u.name === State.me.name ? null : el('button', { class: 'fd-button fd-button--transparent fd-button--negative', type: 'button',
        onclick: async () => {
          if (!window.confirm(t('Benutzer {0} wirklich löschen?', [u.name]))) return;
          try { await api('users/' + encodeURIComponent(u.name), { method: 'DELETE' }); message($('usr-list-msg'), ''); loadUsers(); }
          catch (err) { message($('usr-list-msg'), explain(err.message), true); }
        } }, t('Löschen')));
    return el('tr', { class: 'fd-table__row' },
      cell(el('div', { class: 'doc-title' }, u.display_name || u.name), el('div', { class: 'doc-folder' }, u.name)),
      cell(el('span', { class: 'role-badge' }, u.is_admin ? t('Admin') : t('Benutzer'))),
      cell(areas),
      cell(el('span', { class: 'mfa-badge' + (u.totp ? '' : ' is-off') }, u.totp ? t('an') : t('aus'))),
      actions);
  }));
}

// ----------------------------------------------------------------- areas (as in Ordnung!)

async function areasChanged() {
  State.areas = await api('areas');
  State.me = await api('me');
  renderChips();
  await renderAreaRows();
  if (!$('usr-form').hidden) $('usr-form').hidden = true;      // its list of areas is out of date
  loadTree(); loadPlaceNames(); reload();
}

async function renderAreaRows() {
  const users = await api('users');
  $('area-form').hidden = State.me.readonly;
  $('area-rows').replaceChildren(...State.areas.map((area) => {
    const who = users.filter((u) => !u.is_admin && u.areas[area.name])
      .map((u) => `${u.display_name || u.name} (${PERMS[u.areas[area.name]]})`).join(', ') || t('nur Administratoren');
    const box = (field, label) => el('input', { type: 'checkbox', checked: area[field], 'aria-label': `${area.name}: ${label}`,
      onchange: async (event) => {
        try {
          await api('areas/' + encodeURIComponent(area.name), { method: 'PATCH',
            body: JSON.stringify({ places: area.places, dates: area.dates, [field]: event.target.checked }) });
          message($('area-msg'), '');
          await areasChanged();
        } catch (err) { event.target.checked = area[field]; message($('area-msg'), explain(err.message), true); }
      } });
    return el('tr', { class: 'fd-table__row' },
      cell(el('span', { class: 'area-dot', style: `--dot:${areaColor(area.name)}` }), area.name),
      cell(number(area.count)), cell(who), cell(box('places', t('braucht Ort'))), cell(box('dates', t('braucht Datum'))),
      el('td', { class: 'fd-table__cell usr-actions' }, area.count || State.me.readonly ? null : el('button', {
        class: 'fd-button fd-button--transparent fd-button--negative', type: 'button',
        onclick: async () => {
          if (!window.confirm(t('Bereich {0} mit seinem Ordner wirklich löschen?', [area.name]))) return;
          try { await api('areas/' + encodeURIComponent(area.name), { method: 'DELETE' }); message($('area-msg'), ''); await areasChanged(); }
          catch (err) { message($('area-msg'), explain(err.message), true); }
        } }, t('Löschen'))));
  }));
}

$('area-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const made = await api('areas', { method: 'POST', body: JSON.stringify({
      name: $('area-name').value.trim(), places: $('area-places').checked, dates: $('area-dates').checked }) });
    $('area-name').value = '';
    $('area-places').checked = $('area-dates').checked = true;
    State.active.add(made.name);
    store.set('areas', [...State.active]);
    message($('area-msg'), t('Bereich {0} angelegt. Rechte vergibst du unten beim Benutzer.', [made.name]));
    await areasChanged();
  } catch (err) { message($('area-msg'), explain(err.message), true); }
});

function editUser(u) {
  editing = u ? u.name : null;
  message($('usr-msg'), '');
  $('usr-form-title').textContent = u ? t('Benutzer {0} bearbeiten', [u.name]) : t('Neuer Benutzer');
  $('usr-name').value = u ? u.name : '';
  $('usr-name').disabled = Boolean(u);
  $('usr-display').value = u ? u.display_name : '';
  $('usr-pass').value = '';
  $('usr-pass').required = !u;
  $('usr-pass').placeholder = u ? t('leer lassen = unverändert') : t('mindestens 12 Zeichen');
  $('usr-admin').checked = u ? u.is_admin : false;
  $('usr-signout').checked = false;
  $('usr-reset-totp').checked = false;
  document.querySelectorAll('.usr-existing').forEach((node) => { node.hidden = !u; });
  document.querySelectorAll('.usr-mfa-reset').forEach((node) => { node.hidden = !(u && u.totp); });
  const box = $('usr-areas');
  box.replaceChildren();
  for (const area of State.areas) {
    const select = el('select', { dataset: { area: area.name }, 'aria-label': t('Recht für {0}', [area.name]) },
      ...Object.entries(PERMS).map(([value, text]) => el('option', { value }, text)));
    select.value = (u && u.areas[area.name]) || 'none';
    box.append(el('span', {}, el('span', { class: 'area-dot', style: `--dot:${areaColor(area.name)}` }), area.name), select);
  }
  syncAdminAreas();
  $('usr-form').hidden = false;
  (u ? $('usr-display') : $('usr-name')).focus();
}

function syncAdminAreas() {
  $('usr-areas').querySelectorAll('select').forEach((s) => { s.disabled = $('usr-admin').checked; });
}

$('usr-new').addEventListener('click', () => editUser(null));
$('usr-cancel').addEventListener('click', () => { $('usr-form').hidden = true; });
$('usr-admin').addEventListener('change', syncAdminAreas);
$('usr-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const areas = {};
  $('usr-areas').querySelectorAll('select').forEach((s) => { if (s.value !== 'none') areas[s.dataset.area] = s.value; });
  const body = { display_name: $('usr-display').value, is_admin: $('usr-admin').checked, areas };
  if ($('usr-pass').value) body.password = $('usr-pass').value;
  try {
    if (editing) {
      if ($('usr-signout').checked) body.sign_out = true;
      if ($('usr-reset-totp').checked) body.reset_totp = true;
      await api('users/' + encodeURIComponent(editing), { method: 'PATCH', body: JSON.stringify(body) });
    } else {
      body.name = $('usr-name').value.trim().toLowerCase();
      await api('users', { method: 'POST', body: JSON.stringify(body) });
    }
    $('usr-form').hidden = true;
    await loadUsers();
    if (editing === State.me.name) State.me = await api('me');
  } catch (err) { message($('usr-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- second factor, devices

function mfaView(mode) {      // off | setup | codes | on
  $('mfa-start').hidden = mode !== 'off';
  $('mfa-setup').hidden = mode !== 'setup';
  $('mfa-codes').hidden = mode !== 'codes';
  $('mfa-on').hidden = mode !== 'on';
  document.querySelectorAll('#mfa-card .form-msg').forEach((node) => { node.textContent = ''; });
}

async function loadAccountSecurity() {
  if (State.me.name === 'api') { $('mfa-card').hidden = true; return; }
  if (!$('mfa-codes').hidden || !$('mfa-setup').hidden) { loadSessions(); return; }     // in the middle of setting up: keep it
  State.me = await api('me');
  $('mfa-state').textContent = State.me.totp
    ? t('Eingeschaltet. Von außen fragt Augenblick! nach dem Code aus der App. Noch {0} Wiederherstellungscodes übrig.', [State.me.recovery_left])
    : t('Nicht eingerichtet. Mit einem zweiten Faktor reicht ein gestohlenes Passwort nicht mehr, um von außen hineinzukommen.');
  mfaView(State.me.totp ? 'on' : 'off');
  loadSessions();
}

async function loadSessions() {
  const list = await api('me/sessions');
  $('ses-rows').replaceChildren(...list.map((s) => {
    const where = [s.external ? t('von außen') : t('Heimnetz'), s.ip, s.mfa ? t('mit Code') : ''].filter(Boolean).join(' · ');
    const end = s.current ? null : el('button', { class: 'fd-button fd-button--transparent', type: 'button', onclick: async () => {
      await api('me/sessions/' + encodeURIComponent(s.id), { method: 'DELETE' });
      loadSessions();
    } }, t('Abmelden'));
    return el('tr', { class: 'fd-table__row', title: s.agent },
      cell(el('div', { class: 'doc-title' }, s.device, s.current ? el('span', { class: 'mfa-badge' }, t('dieses Gerät')) : null),
        el('div', { class: 'doc-folder' }, where)),
      cell(el('div', { class: 'doc-folder' }, t('zuletzt {0}', [clock(s.last_seen)])), el('div', { class: 'doc-folder' }, t('angemeldet {0}', [clock(s.created)]))),
      el('td', { class: 'fd-table__cell ses-act' }, end));
  }));
  $('ses-others').hidden = list.length < 2;
}

function showRecoveryCodes(codes) {
  $('mfa-code-list').textContent = codes.join('\n');
  mfaView('codes');
}

$('mfa-start').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const answer = await api('me/totp/setup', { method: 'POST', body: JSON.stringify({ password: $('mfa-pass').value }) });
    $('mfa-pass').value = '';
    $('mfa-qr').src = answer.qr;
    $('mfa-secret').textContent = answer.secret;
    mfaView('setup');
    $('mfa-code').value = '';
    $('mfa-code').focus();
  } catch (err) { message($('mfa-msg-start'), explain(err.message), true); }
});
$('mfa-abort').addEventListener('click', () => mfaView('off'));
$('mfa-confirm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const answer = await api('me/totp/enable', { method: 'POST', body: JSON.stringify({ code: $('mfa-code').value }) });
    showRecoveryCodes(answer.recovery_codes);
  } catch (err) { message($('mfa-msg-confirm'), explain(err.message), true); }
});
$('mfa-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('mfa-code-list').textContent); message($('mfa-msg-codes'), t('Kopiert.')); }
  catch { /* marking the list by hand still works */ }
});
$('mfa-done').addEventListener('click', () => { $('mfa-codes').hidden = true; loadAccountSecurity(); });
$('mfa-renew').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const answer = await api('me/totp/recovery', { method: 'POST', body: JSON.stringify({ code: $('mfa-renew-code').value }) });
    $('mfa-renew-code').value = '';
    showRecoveryCodes(answer.recovery_codes);
  } catch (err) { message($('mfa-msg-renew'), explain(err.message), true); }
});
$('mfa-off-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!window.confirm(t('Zweiten Faktor wirklich ausschalten? Von außen reicht dann wieder das Passwort.'))) return;
  try {
    await api('me/totp/disable', { method: 'POST', body: JSON.stringify({ password: $('mfa-off-pass').value, code: $('mfa-off-code').value }) });
    $('mfa-off-pass').value = '';
    $('mfa-off-code').value = '';
    await loadAccountSecurity();
    message($('mfa-msg-start'), t('Der zweite Faktor ist aus.'));
  } catch (err) { message($('mfa-msg-off'), explain(err.message), true); }
});
$('ses-others').addEventListener('click', async () => {
  const answer = await api('me/sessions/others', { method: 'POST' });
  message($('ses-msg'), answer.signed_out === 1 ? t('1 Gerät abgemeldet.') : t('{0} Geräte abgemeldet.', [answer.signed_out]));
  loadSessions();
});

// ----------------------------------------------------------------- security

async function loadSecurity() {
  const security = await api('security');
  $('sec-where').textContent = State.me.external ? t('Du bist gerade von außen angemeldet.') : t('Du bist gerade im Heimnetz angemeldet.');
  $('sec-nets').value = security.internal_networks;
  $('sec-nets').placeholder = security.networks_default;
  $('sec-nets-here').textContent = t('Deine Adresse gerade: {0} ({1}).', [security.client_ip, State.me.external ? t('zählt als außen') : t('zählt als Heimnetz')]);
  $('sec-ext').checked = security.require_2fa_external;
  $('sec-int').checked = security.require_2fa_internal;
  $('sec-url').value = security.ntfy_url;
  $('sec-token').value = '';
  $('sec-token').placeholder = security.has_ntfy_token ? t('gespeichert – leer lassen = unverändert') : 'tk_…';
  $('sec-clear-token').hidden = !security.has_ntfy_token;
  $('sec-n-ext').checked = security.notify_external_login;
  $('sec-n-lock').checked = security.notify_lockout;
  $('sec-n-2fa').checked = security.notify_2fa_changes;
  $('sec-without').hidden = !security.without_2fa.length;
  $('sec-without').textContent = t('Noch ohne zweiten Faktor: {0}. Diese Konten kommen dann nur noch im Heimnetz hinein.', [security.without_2fa.join(', ')]);
}

async function saveSecurity(extra = {}) {
  const body = {
    internal_networks: $('sec-nets').value.trim(),
    require_2fa_external: $('sec-ext').checked,
    require_2fa_internal: $('sec-int').checked,
    ntfy_url: $('sec-url').value.trim(),
    notify_external_login: $('sec-n-ext').checked,
    notify_lockout: $('sec-n-lock').checked,
    notify_2fa_changes: $('sec-n-2fa').checked,
    ...extra,
  };
  if ($('sec-token').value.trim()) body.ntfy_token = $('sec-token').value.trim();
  await api('security', { method: 'PUT', body: JSON.stringify(body) });
  await loadSecurity();
}

$('sec-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await saveSecurity(); message($('sec-msg'), t('Gespeichert.')); }
  catch (err) { message($('sec-msg'), explain(err.message), true); }
});
$('sec-clear-token').addEventListener('click', async () => {
  try { await saveSecurity({ clear_ntfy_token: true }); message($('sec-msg'), t('Token entfernt.')); }
  catch (err) { message($('sec-msg'), explain(err.message), true); }
});
$('sec-test').addEventListener('click', async () => {
  try {
    await saveSecurity();
    await api('security/test', { method: 'POST' });
    message($('sec-msg'), t('Testnachricht verschickt.'));
  } catch (err) { message($('sec-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- API key

function showApiKey(token) {
  ApiKey.token = token || '';
  ApiKey.shown = false;
  paintApiKey();
  $('api-copy').hidden = !token;
  $('api-del').hidden = !token;
  $('api-state').textContent = token ? t('Schlüssel ist aktiv.') : t('Kein Schlüssel: Die API ist nur mit einer Anmeldung im Browser nutzbar.');

}
async function loadApiKey() { showApiKey((await api('security/api-token')).api_token); }
$('api-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(ApiKey.token); $('api-state').textContent = t('Schlüssel kopiert.'); }
  catch { ApiKey.shown = true; paintApiKey(); $('api-key').select(); }
});
$('api-new').addEventListener('click', async () => {
  if (ApiKey.token && !window.confirm(t('Neuen Schlüssel erzeugen? Der bisherige funktioniert dann sofort nicht mehr – Skripte, die ihn nutzen, brauchen den neuen.'))) return;
  showApiKey((await api('security/api-token', { method: 'POST' })).api_token);
  $('api-state').textContent = t('Neuer Schlüssel erzeugt.');
});
$('api-del').addEventListener('click', async () => {
  if (!window.confirm(t('Schlüssel löschen? Skripte kommen dann nicht mehr hinein.'))) return;
  showApiKey((await api('security/api-token', { method: 'DELETE' })).api_token);
});

// the door for the phone: one area, its own key – the key is shown once, when it is made
function showUpload(state, key = '', keyId = '') {
  const select = $('up-area');
  select.replaceChildren(el('option', { value: '' }, t('keiner – Upload aus')), ...state.areas.map((name) => el('option', { value: name }, name)));
  select.value = state.areas.includes(state.area) ? state.area : '';
  $('up-url').textContent = location.origin + state.path;
  const when = (seconds) => new Date(seconds * 1000).toLocaleString(LOCALE, { dateStyle: 'short', timeStyle: 'short' });
  const called = (entry) => entry.name || t('ohne Namen');
  const change = async (entry, body) => {
    try { showUpload(await api('upload/keys/' + entry.id, { method: 'PATCH', body: JSON.stringify(body) })); refreshInbox(); }
    catch (err) { $('up-state').textContent = explain(err.message); }
  };
  $('up-keys').replaceChildren(...state.keys.map((entry) => el('tr', { class: 'fd-table__row' },
    el('td', {}, el('input', { class: 'fd-input', value: entry.name, maxlength: '40', placeholder: t('Name'), 'aria-label': t('Name des Schlüssels'),
      onchange: (event) => change(entry, { name: event.target.value }) })),
    el('td', {}, (() => {
      const where = el('select', { class: 'fd-input', 'aria-label': t('Bereich für {0}', [called(entry)]), onchange: (event) => change(entry, { area: event.target.value }) },
        el('option', { value: '' }, state.area ? t('wie oben ({0})', [state.area]) : t('wie oben')), ...state.areas.map((name) => el('option', { value: name }, name)));
      where.value = state.areas.includes(entry.area) ? entry.area : '';
      return where;
    })()),
    el('td', { class: 'up-used' }, t('vom {0}', [when(entry.made)]), el('br'), entry.last ? t('{0} Dateien, zuletzt {1}', [number(entry.count), when(entry.last)]) : t('noch nichts hochgeladen')),
    el('td', {}, el('button', { class: 'fd-button fd-button--compact', type: 'button', onclick: async () => {
      if (!window.confirm(t('Neuen Schlüssel für „{0}“ erzeugen? Der bisherige funktioniert dann sofort nicht mehr – die App braucht den neuen.', [called(entry)]))) return;
      const made = await api(`upload/keys/${entry.id}/renew`, { method: 'POST' });
      showUpload(made, made.key, made.key_id);
    } }, t('Neu erzeugen')), ' ', el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button', onclick: async () => {
      if (!window.confirm(t('Schlüssel „{0}“ löschen? Dieses Telefon kann dann nichts mehr hochladen.', [called(entry)]))) return;
      showUpload(await api('upload/keys/' + entry.id, { method: 'DELETE' })); refreshInbox();
    } }, t('Löschen'))))));
  $('up-fresh').hidden = !key;
  $('up-key').value = key;
  const fresh = state.keys.find((entry) => entry.id === keyId);
  $('up-fresh-for').textContent = fresh ? t('Neuer Schlüssel für „{0}“:', [called(fresh)]) : t('Neuer Schlüssel:');
  const open = state.keys.filter((entry) => entry.area || state.area).length;
  let text = !state.has_key ? t('Kein Schlüssel: Die Tür ist zu.')
    : !open ? t('Schlüssel vorhanden, aber kein Bereich gewählt: Die Tür ist zu.')
      : `${state.keys.length === 1 ? t('Ein Schlüssel') : t('{0} Schlüssel', [number(state.keys.length)])}.`
        + (state.last ? t(' Bisher {0} Dateien, zuletzt {1}.', [number(state.count), when(state.last)]) : t(' Noch nichts hochgeladen.'));
  if (state.readonly) text += t(' Achtung: Die Fotos sind nur lesend eingebunden, Uploads werden abgewiesen.');
  $('up-state').textContent = text;
}
async function refreshInbox() {       // the areas uploads go to are the inbox
  State.areas = await api('areas');
  State.stale = true;
  renderChips();
}
async function loadUpload() { showUpload(await api('upload')); }
$('up-area').addEventListener('change', async (event) => {
  showUpload(await api('upload', { method: 'PUT', body: JSON.stringify({ area: event.target.value }) }));
  refreshInbox();
});
$('up-copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('up-key').value); $('up-state').textContent = t('Schlüssel kopiert.'); }
  catch { $('up-key').select(); }
});
$('up-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const made = await api('upload/keys', { method: 'POST', body: JSON.stringify({ name: $('up-name').value }) });
    $('up-name').value = '';
    showUpload(made, made.key, made.key_id);
  } catch (err) { $('up-state').textContent = explain(err.message); }
});

$('btn-rescan').addEventListener('click', async () => {
  await api('rescan', { method: 'POST' });
  setTimeout(() => { pollStatus(); showSettings(); }, 1500);
});

// ----------------------------------------------------------------- changing

const PROBLEMS = [
  ['invalid folder name', t('So kann ein Ordner nicht heißen.')],
  ['folder exists', t('Diesen Ordner gibt es schon.')],
  ['folder is not empty', t('Der Ordner ist nicht leer.')],
  ['unknown folder', t('Diesen Ordner gibt es nicht (mehr).')],
  ['invalid area name', t('So kann ein Bereich nicht heißen: Buchstaben, Ziffern, Leerzeichen, Punkt, Binde- und Unterstrich, höchstens 40 Zeichen.')],
  ['area exists', t('Diesen Bereich oder einen Ordner dieses Namens gibt es schon.')],
  ['area holds', t('Der Bereich ist nicht leer (auch der Papierkorb zählt).')],
  ['cannot create the folder', t('Der Ordner ließ sich nicht anlegen – darf Augenblick! ins Foto-Verzeichnis schreiben?')],
  ['read-only', t('Augenblick! darf in diese Fotos nicht schreiben.')],
  ['no write access', t('Dir fehlt das Schreibrecht für diesen Bereich.')],
  ['only JPEG', t('Drehen geht bisher nur bei JPEG- und HEIC-Bildern.')],
  ['no turn field', t('Dieses Bild lässt sich hier nicht drehen: Die Datei hat kein eigenes Drehfeld.')],
  ['only pictures', t('Videos lassen sich noch nicht ändern.')],
  ['raw files', t('Rohdateien (DNG) bleiben so, wie die Kamera sie geschrieben hat.')],
  ['only pictures can be worked on', t('Bearbeiten geht nur bei Fotos – nicht bei Videos, Rohdateien und GIFs.')],
  ['nothing to change', t('Es ist nichts verändert.')],
  ['the original of this picture', t('Das Original zu diesem Bild liegt nicht mehr daneben – von hier aus lässt es sich nicht neu bearbeiten.')],
  ['not worked on here', t('Dieses Bild wurde nicht hier bearbeitet.')],
  ['uploads in pieces', t('Hochladen in Teilstücken geht nicht – in der App „Hochladen in Chunks“ ausschalten.')],
  ['the file was changed again', t('Das Bild wurde danach noch einmal geändert – nimm zuerst die spätere Änderung zurück.')],
  ['already taken back', t('Das ist schon zurückgenommen.')],
  ['file is not writable', t('Die Datei ist schreibgeschützt.')],
  ['file not found', t('Die Datei gibt es nicht mehr.')],
  ['photo not found', t('Das Bild gibt es nicht mehr.')],
  ['invalid date', t('Das Datum ist ungültig.')],
  ['entry not found', t('Den Eintrag gibt es nicht mehr.')],
  ['wrong password', t('Das aktuelle Passwort stimmt nicht.')],
  ['face search is switched off', t('Die Suche nach Gesichtern ist ausgeschaltet – einschalten unter Einstellungen → Personen.')],
  ['unknown building block', t('Diesen Baustein gibt es nicht.')],
  ['unbalanced braces', t('Im Schema fehlt eine geschweifte Klammer.')],
  ['characters that file names cannot have', t('Im Schema stehen Zeichen, die in Dateinamen nicht erlaubt sind.')],
  ['invalid folder', t('Das Ordner-Schema ist ungültig.')],
  ['a rule needs a folder', t('Jede Regel braucht einen Ordner, für den sie gilt.')],
  ['two rules for the same folder', t('Für einen Ordner gibt es zwei Regeln.')],
  ['the place is taken', t('Am alten Platz liegt inzwischen etwas anderes.')],
  ['photos stay inside their area', t('Bilder bleiben in ihrem Bereich.')],
  ['another job is running', t('Es läuft gerade schon ein längerer Auftrag.')],
  ['choose a folder inside the area', t('Bitte einen Ordner innerhalb eines Bereichs wählen.')],
  ['AI is disabled', t('Die KI ist ausgeschaltet.')],
  ['AI is not allowed for this area', t('Für diesen Bereich ist die KI gesperrt.')],
  ['monthly limit reached', t('Das Monatslimit für die KI ist erreicht.')],
  ['no API key', t('Es ist kein API-Schlüssel hinterlegt.')],
  ['API key rejected', t('Der Anbieter lehnt den API-Schlüssel ab.')],
  ['not an Anthropic key', t('Das sieht nicht nach einem Anthropic-Schlüssel aus (er beginnt mit sk-ant-).')],
  ['no server address', t('Es fehlt die Adresse des Servers.')],
  ['cannot reach', t('Der KI-Dienst ist nicht erreichbar.')],
  ['unknown model', t('Dieses Modell kennt der Anbieter nicht.')],
  ['rate limit', t('Der Anbieter bremst gerade – bitte gleich noch einmal versuchen.')],
  ['the model declined', t('Das Modell hat die Anfrage abgelehnt.')],
  ['the answer was not in the agreed form', t('Die KI hat nicht im vereinbarten Format geantwortet. Versuch es noch einmal oder nimm ein anderes Modell.')],
  ['instruction name used twice', t('Zwei Anweisungen tragen denselben Namen.')],
  ['instruction without', t('Jede Anweisung braucht einen Namen und einen Text.')],
  ['unknown instruction', t('Diese Anweisung gibt es nicht mehr.')],
  ['the picture cannot be read', t('Das Bild lässt sich nicht lesen.')],
  ['invalid code', t('Der Code stimmt nicht oder wurde schon benutzt.')],
  ['second factor already on', t('Der zweite Faktor ist schon eingeschaltet.')],
  ['set up your own second factor first', t('Richte zuerst für dein eigenes Konto einen zweiten Faktor ein.')],
  ['would no longer count as home', t('Deine eigene Adresse würde dann nicht mehr als Heimnetz zählen.')],
  ['too short', t('Das Passwort braucht mindestens 12 Zeichen.')],
  ['user exists', t('Den Benutzer gibt es schon.')],
  ['invalid user name', t('Der Anmeldename darf nur Kleinbuchstaben, Ziffern, Punkt und Strich enthalten.')],
  ['cannot delete yourself', t('Du kannst dich nicht selbst löschen.')],
  ['cannot remove your own admin role', t('Die eigene Administrator-Rolle lässt sich nicht abgeben.')],
  ['last administrator', t('Ein Administrator muss bleiben.')],
  ['no date to shift', t('Ohne Aufnahmedatum gibt es nichts zu verschieben – setze das Datum stattdessen.')],
  ['invalid face box', t('Der Rahmen um das Gesicht ist ungültig.')],
  ['invalid address', t('Die Adresse stimmt nicht – sie beginnt mit http:// oder https://.')],
  ['api key missing', t('Bitte den API-Schlüssel eintragen.')],
  ['immich refused the key', t('Immich lehnt den Schlüssel ab – oder ihm fehlt das Recht, Personen, Gesichter und Bilder zu lesen.')],
  ['immich not reachable', t('Immich ist unter dieser Adresse nicht zu erreichen.')],
  ['immich answered', t('Immich hat mit einem Fehler geantwortet.')],
  ['immich saw this picture turned another way', t('Immich hat das Bild anders gedreht gesehen als es hier steht – die Rahmen würden nicht passen.')],
  ['preview is out of date', t('Die Vorschau ist nicht mehr aktuell – bitte noch einmal „Vorschau“.')],
  ['person not found', t('Diese Person steht in keinem Bild (mehr).')],
  ['choose another name', t('Bitte einen anderen Namen wählen.')],
  ['face not found', t('Dieses Gesicht gibt es nicht mehr – oder dir fehlt das Schreibrecht für sein Bild.')],
  ['exiftool refused', t('exiftool kann die Angaben in dieser Datei nicht ändern, das Bild ist unverändert.')],
];
function explain(detail) {
  const text = String(detail);
  for (const [start, german] of PROBLEMS) if (text.includes(start)) return german;
  if (text.startsWith('check failed')) return t('Die Prüfung nach dem Schreiben ist gescheitert, das Bild ist unverändert. ({0})', [text]);
  return text;
}

function parsePlace(text) {
  const value = text.trim();
  let match = value.match(/^(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)$/);
  if (!match) {
    match = value.match(/^(-?\d+,\d+)\s*(?:;|,\s+|\s+)\s*(-?\d+,\d+)$/);
    if (match) match = [0, match[1].replace(',', '.'), match[2].replace(',', '.')];
  }
  if (!match) return null;
  const lat = Number(match[1]); const lon = Number(match[2]);
  return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? { lat, lon } : null;
}
const words = (text) => text.split(',').map((w) => w.trim()).filter(Boolean);

/* A changed photo comes back from the server; a turned one has a new key. Bring grid and viewer in line. */
function adopt(photo, oldKey) {
  State.dirty = true;
  for (const items of State.items.values()) {
    const item = items.find((i) => i.key === oldKey);
    if (item) { item.key = photo.key; item.o = photo.orientation || 1; }
  }
  if (State.selected.delete(oldKey)) State.selected.add(photo.key);
  const button = document.querySelector(`.tile[data-key="${oldKey}"]`);
  if (button) {
    button.dataset.key = photo.key;
    const img = button.querySelector('img');
    if (img) img.src = thumbUrl({ key: photo.key, o: photo.orientation });
  }
}

async function turnShown(degrees) {
  const old = Viewer.key;
  for (const id of ['v-left', 'v-right']) $(id).disabled = true;
  message($('v-msg'), t('Dreht …'));
  try {
    const photo = await api(`photos/${old}/rotate`, { method: 'POST', body: JSON.stringify({ degrees }) });
    adopt(photo, old);
    if (Viewer.key === old) {
      Viewer.key = photo.key;
      showPhoto(photo);
      message($('v-msg'), photo.orientation > 1
        ? t('Per Markierung gedreht – verlustfrei ging es bei diesem Bild nicht. Warum, steht unter Einstellungen → Änderungen.')
        : t('Verlustfrei gedreht.'));
    }
  } catch (err) { message($('v-msg'), explain(err.message), true); }
  for (const id of ['v-left', 'v-right']) $(id).disabled = false;
}
$('v-left').addEventListener('click', () => turnShown(270));
$('v-right').addEventListener('click', () => turnShown(90));

$('v-edit').addEventListener('click', () => {
  const photo = Viewer.photo;
  $('e-taken').value = photo.taken;
  $('e-place').value = photo.lat === null ? '' : `${photo.lat}, ${photo.lon}`;
  $('e-desc').value = photo.description || '';
  $('e-words').value = photo.keywords.join(', ');
  $('e-albums').value = photo.albums.join(', ');
  $('e-rating').value = String(photo.rating || 0);
  $('v-form').hidden = false;
  $('v-facts').hidden = true;
  message($('v-msg'), '');
  $('e-taken').focus();
});
$('e-cancel').addEventListener('click', () => { $('v-form').hidden = true; $('v-facts').hidden = false; });

$('v-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const photo = Viewer.photo;
  const change = {};
  const taken = $('e-taken').value.length === 16 ? $('e-taken').value + ':00' : $('e-taken').value;
  if (taken !== photo.taken) change.taken = taken;
  const place = $('e-place').value.trim();
  if (!place) { if (photo.lat !== null) change.clear_place = true; }
  else {
    const spot = parsePlace(place);
    if (!spot) { message($('v-msg'), t('Den Ort bitte als Breite und Länge angeben, etwa 49.84512, 7.86301.'), true); return; }
    if (photo.lat === null || Math.abs(spot.lat - photo.lat) > 1e-6 || Math.abs(spot.lon - photo.lon) > 1e-6) Object.assign(change, spot);
  }
  if ($('e-desc').value.trim() !== (photo.description || '')) change.description = $('e-desc').value;
  const list = words($('e-words').value);
  if (list.join('\n') !== photo.keywords.join('\n')) change.keywords = list;
  if (Number($('e-rating').value) !== (photo.rating || 0)) change.rating = Number($('e-rating').value);
  const albums = words($('e-albums').value);
  if (albums.join('\n') !== photo.albums.join('\n')) change.albums = albums;
  if (!Object.keys(change).length) { $('v-form').hidden = true; $('v-facts').hidden = false; return; }
  message($('v-msg'), t('Schreibt …'));
  try {
    const updated = await api(`photos/${photo.key}`, { method: 'PATCH', body: JSON.stringify(change) });
    adopt(updated, photo.key);
    if (Viewer.key === photo.key) { Viewer.key = updated.key; showPhoto(updated); message($('v-msg'), t('Ins Bild geschrieben.')); }
    if (change.albums) loadAlbums();
    if (updated.misfit && !photo.misfit && Viewer.key === updated.key
      && window.confirm(t('Das Bild passt jetzt nicht mehr an seinen Platz.\n\n{0}\n→ {1}\n\nGleich einsortieren?', [updated.path, updated.target]))) sortShown();
  } catch (err) { message($('v-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- map

const Atlas = { map: null, pin: null, spot: null, done: null };

function setSpot(lat, lon, fly) {
  lon = ((lon + 180) % 360 + 360) % 360 - 180;       // the map repeats sideways
  Atlas.spot = { lat: Number(lat.toFixed(6)), lon: Number(lon.toFixed(6)) };
  if (!Atlas.pin) {
    Atlas.pin = L.marker([lat, lon], { draggable: true, keyboard: false }).addTo(Atlas.map);
    Atlas.pin.on('dragend', () => { const at = Atlas.pin.getLatLng(); setSpot(at.lat, at.lng, false); });
  } else Atlas.pin.setLatLng([lat, lon]);
  if (fly) Atlas.map.setView([lat, lon], Math.max(Atlas.map.getZoom(), 14));
  $('map-where').textContent = `${Atlas.spot.lat.toFixed(5)}, ${Atlas.spot.lon.toFixed(5)}`;
  $('map-ok').disabled = false;
}

/* Opens the map; the promise answers with {lat, lon} or null. */
function pickPlace(start) {
  $('map-dialog').showModal();
  if (!Atlas.map) {
    L.Icon.Default.imagePath = 'vendor/leaflet/images/';
    Atlas.map = L.map('map', { worldCopyJump: true });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
    }).addTo(Atlas.map);
    Atlas.map.on('click', (event) => setSpot(event.latlng.lat, event.latlng.lng, false));
  }
  if (Atlas.pin) { Atlas.pin.remove(); Atlas.pin = null; }
  Atlas.spot = null;
  $('map-ok').disabled = true;
  $('map-hits').replaceChildren();
  $('map-q').value = '';
  message($('map-where'), t('Klick in die Karte setzt den Ort.'));
  Atlas.map.invalidateSize();
  const last = store.get('place', null);
  if (start) { Atlas.map.setView([start.lat, start.lon], 15); setSpot(start.lat, start.lon, false); }
  else if (last) Atlas.map.setView([last.lat, last.lon], 12);     // where you were last time
  else Atlas.map.setView([51.2, 10.4], 6);
  return new Promise((resolve) => { Atlas.done = resolve; });
}

function closeMap(result) {
  if (result) store.set('place', result);
  if ($('map-dialog').open) $('map-dialog').close();
  if (Atlas.done) { const done = Atlas.done; Atlas.done = null; done(result); }
}
$('map-ok').addEventListener('click', () => closeMap(Atlas.spot));
$('map-cancel').addEventListener('click', () => closeMap(null));
$('map-dialog').addEventListener('close', () => closeMap(null));     // Esc

$('map-search').addEventListener('submit', async (event) => {
  event.preventDefault();
  const q = $('map-q').value.trim();
  if (!q) return;
  const box = $('map-hits');
  box.replaceChildren(el('span', { class: 'field-hint' }, t('Sucht …')));
  let hits;
  try {
    const res = await fetch('https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&accept-language=de&q=' + encodeURIComponent(q),
      { credentials: 'omit', referrerPolicy: 'no-referrer' });
    if (!res.ok) throw new Error(res.status);
    hits = await res.json();
  } catch { box.replaceChildren(el('span', { class: 'field-hint' }, t('Die Ortssuche ist gerade nicht erreichbar.'))); return; }
  if (!hits.length) { box.replaceChildren(el('span', { class: 'field-hint' }, t('Nichts gefunden.'))); return; }
  box.replaceChildren(...hits.map((hit) => el('button', { type: 'button', title: hit.display_name,
    onclick: () => setSpot(Number(hit.lat), Number(hit.lon), true) }, hit.display_name)));
  if (hits.length === 1) setSpot(Number(hits[0].lat), Number(hits[0].lon), true);
});

async function mapInto(input) {
  const result = await pickPlace(parsePlace(input.value));
  if (result) input.value = `${result.lat}, ${result.lon}`;
  input.focus();
}
$('e-map').addEventListener('click', () => mapInto($('e-place')));
$('b-map').addEventListener('click', () => mapInto($('b-place')));

// ----------------------------------------------------------------- several at once

function markSelection() {
  for (const button of document.querySelectorAll('.tile')) {
    const hit = State.selected.has(button.dataset.key);
    button.classList.toggle('is-selected', hit);
    if (State.selecting) button.setAttribute('aria-pressed', String(hit)); else button.removeAttribute('aria-pressed');
  }
  const n = State.selected.size;
  $('sel-count').textContent = n === 0 ? t('Nichts ausgewählt') : n === 1 ? t('1 Bild ausgewählt') : t('{0} Bilder ausgewählt', [number(n)]);
  for (const id of ['sel-edit', 'sel-left', 'sel-right', 'sel-none', 'sel-delete', 'sel-propose', 'sel-take']) $(id).disabled = n === 0;
}

let lastPicked = null;
function toggleSelected(button, range) {
  const tiles = [...document.querySelectorAll('.tile')];
  const at = tiles.indexOf(button);
  if (at < 0) return;
  const from = range && lastPicked ? tiles.findIndex((t) => t.dataset.key === lastPicked) : -1;
  if (from >= 0) for (const t of tiles.slice(Math.min(from, at), Math.max(from, at) + 1)) State.selected.add(t.dataset.key);
  else if (State.selected.has(button.dataset.key)) State.selected.delete(button.dataset.key);
  else State.selected.add(button.dataset.key);
  lastPicked = button.dataset.key;
  markSelection();
}

function setSelecting(on) {
  State.selecting = on;
  if (!on) State.selected.clear();
  lastPicked = null;
  $('view-library').classList.toggle('is-selecting', on);
  $('select-bar').hidden = !on;
  $('btn-select').hidden = on || !State.me.write.length;
  message($('sel-msg'), '');
  markSelection();
}
$('btn-select').addEventListener('click', () => setSelecting(true));
$('sel-done').addEventListener('click', () => setSelecting(false));
$('sel-none').addEventListener('click', () => { State.selected.clear(); markSelection(); });
$('sel-all').addEventListener('click', async (event) => {       // every photo of the view, month by month
  event.target.disabled = true;
  try {
    let done = 0;
    for (const { month } of State.months) {
      for (const item of await monthItems(month)) State.selected.add(item.key);
      done += 1;
      if (done % 10 === 0) message($('sel-msg'), t('Wählt aus: {0} von {1} Monaten …', [number(done), number(State.months.length)]));
    }
    message($('sel-msg'), '');
  } catch (err) { message($('sel-msg'), explain(err.message), true); }
  event.target.disabled = false;
  markSelection();
});

const randomHex = () => [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, '0')).join('');

/* Sends the keys in small portions, so there is progress to show and no request runs for minutes. */
async function runMany(path, payload, tell, verb = t('geändert')) {
  const keys = [...State.selected];
  const batch = randomHex();
  let good = 0; const bad = [];
  for (let i = 0; i < keys.length; i += 10) {
    tell(t('{0} von {1} …', [number(i), number(keys.length)]));
    let results;
    try { results = await api(path, { method: 'POST', body: JSON.stringify({ ...payload, keys: keys.slice(i, i + 10), batch }) }); }
    catch (err) { results = keys.slice(i, i + 10).map((key) => ({ key, ok: false, error: err.message })); }
    for (const result of results) {
      if (!result.ok) bad.push(result.error);
      else { good += 1; if (result.photo) adopt(result.photo, result.key); else State.selected.delete(result.key); }
    }
  }
  State.dirty = false;
  let text = good === 1 ? t('1 Bild {0}.', [verb]) : t('{0} Bilder {1}.', [number(good), verb]);
  if (bad.length) text += t(' {0} nicht: {1}', [number(bad.length), [...new Set(bad.map(explain))].join(' ')]);
  tell(text, bad.length > 0);
  loadTree();
  await reload(true);
  markSelection();
  return bad.length === 0;
}

async function turnSelected(degrees) {
  for (const id of ['sel-left', 'sel-right', 'sel-edit']) $(id).disabled = true;
  await runMany('rotate', { degrees }, (text, isBad) => message($('sel-msg'), text, isBad));
  markSelection();
}
$('sel-left').addEventListener('click', () => turnSelected(270));
$('sel-right').addEventListener('click', () => turnSelected(90));

const batchFields = () => {
  $('b-shift').hidden = $('b-date-mode').value !== 'shift';
  $('b-date').hidden = $('b-date-mode').value !== 'set';
  $('b-place-row').hidden = $('b-place-mode').value !== 'set';
};
$('b-date-mode').addEventListener('change', batchFields);
$('b-place-mode').addEventListener('change', batchFields);
$('sel-edit').addEventListener('click', () => {
  $('batch-form').reset();
  batchFields();
  message($('batch-msg'), '');
  const n = State.selected.size;
  $('batch-title').textContent = n === 1 ? t('1 Bild ändern') : t('{0} Bilder ändern', [number(n)]);
  $('batch-go').disabled = false;
  $('batch').showModal();
});
$('batch-cancel').addEventListener('click', () => $('batch').close());

$('batch-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const msg = $('batch-msg');
  const change = {};
  if ($('b-date-mode').value === 'shift') {
    const amount = Math.round(Number($('b-shift-n').value));
    if (!(amount > 0)) { message(msg, t('Um wie viel soll das Datum verschoben werden?'), true); return; }
    change.shift = amount * Number($('b-shift-unit').value) * Number($('b-shift-dir').value);
  } else if ($('b-date-mode').value === 'set') {
    if (!$('b-date').value) { message(msg, t('Bitte ein Datum angeben.'), true); return; }
    change.taken = $('b-date').value.length === 16 ? $('b-date').value + ':00' : $('b-date').value;
  }
  if ($('b-place-mode').value === 'clear') change.clear_place = true;
  else if ($('b-place-mode').value === 'set') {
    const spot = parsePlace($('b-place').value);
    if (!spot) { message(msg, t('Den Ort bitte als Breite und Länge angeben, etwa 49.84512, 7.86301.'), true); return; }
    Object.assign(change, spot);
  }
  if (words($('b-add').value).length) change.add_keywords = words($('b-add').value);
  if (words($('b-remove').value).length) change.remove_keywords = words($('b-remove').value);
  if (words($('b-album-add').value).length) change.add_albums = words($('b-album-add').value);
  if (words($('b-album-remove').value).length) change.remove_albums = words($('b-album-remove').value);
  if ($('b-rating').value !== '') change.rating = Number($('b-rating').value);
  if (!Object.keys(change).length) { message(msg, t('Es ist nichts zum Ändern angegeben.'), true); return; }
  $('batch-go').disabled = true;
  const fine = await runMany('edit', { change }, (text, isBad) => message(msg, text, isBad));
  if (fine) { message($('sel-msg'), msg.textContent); $('batch').close(); }
  else $('batch-go').disabled = false;
});

// ----------------------------------------------------------------- the inbox, worked through
/* What the phone sent waits in the inbox. The AI may have looked at a picture already (by itself, if that is
   switched on, or when asked); its description and keywords stay a proposal. In the viewer one strikes out what
   does not fit and sorts the picture in – the chosen proposal is written into it, it goes where the rule of the
   chosen area wants it, and the next picture of the inbox comes up. Several chosen pictures go the same way,
   each with all of its proposal. */

const Take = { off: new Set() };       // proposed keywords struck out for the picture on screen
const takeTargets = () => (State.me ? State.areas.filter((a) => !a.inbox && State.me.write.includes(a.name)) : []);

function fillTakeTargets(select, first) {
  const targets = takeTargets();
  const saved = store.get('inboxTarget', '');
  select.replaceChildren(...(first ? [el('option', { value: '' }, first)] : []),
    ...targets.map((a) => el('option', { value: a.name }, a.name)));
  if (!first) select.value = targets.some((a) => a.name === saved) ? saved : targets.length ? targets[0].name : '';
}

function showTake(photo) {
  const box = $('v-inbox');
  box.hidden = !(photo.inbox && photo.can_delete && takeTargets().length);
  if (box.hidden) return;
  fillTakeTargets($('v-inbox-area'));
  const idea = photo.proposal;
  Take.off = new Set();
  $('v-inbox-none').hidden = Boolean(idea);
  $('v-inbox-none').textContent = State.ai.enabled && photo.can_write
    ? t('Die KI hat sich das Bild noch nicht angesehen.') : t('Das Bild wird so einsortiert, wie es ist.');
  $('v-inbox-idea').hidden = !idea;
  $('v-inbox-drop').hidden = !idea;
  if (!idea) return;
  $('v-inbox-from').textContent = t('Vorschlag der KI ({0}{1})', [idea.model || 'KI', cents(idea.cost)]);
  $('v-inbox-desc').textContent = idea.description || '';
  $('v-inbox-desc-on').closest('label').hidden = !idea.description;
  $('v-inbox-desc-on').checked = Boolean(idea.description) && !(photo.description || '').trim();     // one already there wins
  const words = (idea.keywords || []).filter((word) => !photo.keywords.includes(word));
  $('v-inbox-words').replaceChildren(...words.map((word) => {
    const chip = el('button', { class: 'tag inbox-word', type: 'button', 'aria-pressed': 'true', title: t('Anklicken: nicht übernehmen') }, word);
    chip.addEventListener('click', () => {
      const off = !Take.off.has(word);
      if (off) Take.off.add(word); else Take.off.delete(word);
      chip.setAttribute('aria-pressed', String(!off));
      chip.title = off ? t('Anklicken: doch übernehmen') : t('Anklicken: nicht übernehmen');
    });
    return chip;
  }));
  const dated = idea.date && photo.taken_src !== 'exif';
  $('v-inbox-date-row').hidden = !dated;
  $('v-inbox-date-on').checked = false;
  if (dated) {
    const when = new Date(idea.date.taken).toLocaleDateString(LOCALE, idea.date.precision === 'year' ? { year: 'numeric' }
      : idea.date.precision === 'month' ? { month: 'long', year: 'numeric' } : { day: 'numeric', month: 'long', year: 'numeric' });
    $('v-inbox-date').textContent = t('Geschätztes Datum übernehmen: {0}', [when]);
  }
}

function forget(key) {         // a picture that left the view: leafing on skips it
  for (const items of State.items.values()) {
    const at = items.findIndex((i) => i.key === key);
    if (at >= 0) items.splice(at, 1);
  }
  State.selected.delete(key);
}

$('v-inbox-take').addEventListener('click', async () => {
  const photo = Viewer.photo; const area = $('v-inbox-area').value;
  if (!photo || !area) return;
  store.set('inboxTarget', area);
  const idea = photo.proposal;
  const body = { keys: [photo.key], area };
  if (idea) {
    body.description = $('v-inbox-desc-on').checked ? idea.description || '' : '';
    body.keywords = (idea.keywords || []).filter((word) => !Take.off.has(word) && !photo.keywords.includes(word));
    if (!$('v-inbox-date-row').hidden && $('v-inbox-date-on').checked) body.taken = idea.date.taken;
  } else body.take = false;
  $('v-inbox-take').disabled = true;
  message($('v-msg'), t('Wird einsortiert …'));
  try {
    const next = (await neighbour(1)) || (await neighbour(-1));
    const [result] = await api('inbox/accept', { method: 'POST', body: JSON.stringify(body) });
    if (!result.ok) throw new Error(result.error);
    forget(photo.key);
    State.dirty = true;
    try { State.areas = await api('areas'); showInbox(); } catch { /* the next look fixes the number */ }
    if (Viewer.key !== photo.key) return;
    if (next && next.key !== photo.key) {
      await openViewer(next.key, next.month);
      message($('v-msg'), t('Das vorige Bild liegt jetzt in {0}.', [result.photo.folder]));
    } else closeViewer();
  } catch (err) { message($('v-msg'), explain(err.message), true); }
  finally { $('v-inbox-take').disabled = false; }
});

$('v-inbox-drop').addEventListener('click', async () => {
  const photo = Viewer.photo;
  try {
    await api(`photos/${photo.key}/proposal`, { method: 'DELETE' });
    photo.proposal = null;
    showTake(photo);
    message($('v-msg'), t('Vorschlag verworfen.'));
  } catch (err) { message($('v-msg'), explain(err.message), true); }
});

$('sel-take').addEventListener('change', async (event) => {
  const area = event.target.value;
  event.target.value = '';
  const n = State.selected.size;
  if (!area || !n) return;
  if (!window.confirm(n === 1 ? t('Das Bild mit dem Vorschlag der KI nach {0} einsortieren?', [area])
    : t('{0} Bilder mit den Vorschlägen der KI nach {1} einsortieren?', [number(n), area]))) return;
  store.set('inboxTarget', area);
  event.target.disabled = true;
  await runMany('inbox/accept', { area }, (text, isBad) => message($('sel-msg'), text, isBad), t('einsortiert'));
  event.target.disabled = false;
  State.areas = await api('areas');
  showInbox();
});

let proposalTimer = 0;
async function watchProposals() {
  clearTimeout(proposalTimer);
  let state;
  try { state = await api('inbox/proposals', { quiet: true }); } catch { return; }
  const busy = state.running || state.waiting > 0;
  if (busy) message($('sel-msg'), t('Die KI sieht sich die Bilder an: {0} fertig, {1} offen.', [number(state.done), number(state.left + state.waiting)]));
  else if (state.error) message($('sel-msg'), explain(state.error), true);
  else message($('sel-msg'), t('Die Vorschläge der KI sind da – sie stehen in der Großansicht jedes Bildes.'));
  if (busy) proposalTimer = setTimeout(watchProposals, 3000);
}

$('sel-propose').addEventListener('click', async () => {
  const keys = [...State.selected];
  if (!keys.length) return;
  $('sel-propose').disabled = true;
  try {
    let queued = 0;
    for (let i = 0; i < keys.length; i += 500) {
      queued += (await api('inbox/proposals', { method: 'POST', body: JSON.stringify({ keys: keys.slice(i, i + 500) }) })).queued;
    }
    message($('sel-msg'), t('{0} Bilder an die KI gegeben.', [number(queued)]));
    proposalTimer = setTimeout(watchProposals, 1500);
  } catch (err) { message($('sel-msg'), explain(err.message), true); }
  finally { $('sel-propose').disabled = false; }
});

// ----------------------------------------------------------------- sorting

async function sortShown() {
  const key = Viewer.key;
  try {
    const [result] = await api('sort', { method: 'POST', body: JSON.stringify({ keys: [key] }) });
    if (!result.ok) throw new Error(result.error);
    adopt(result.photo, key);
    loadTree();
    if (Viewer.key === key) { showPhoto(result.photo); message($('v-msg'), t('Einsortiert: {0}', [result.photo.path])); }
  } catch (err) { message($('v-msg'), explain(err.message), true); }
}

// handing photos over to another area: the list offers every area you may write to
function fillMoveTargets() {
  const select = $('sel-move');
  const targets = State.areas.filter((a) => State.me.write.includes(a.name));
  select.hidden = targets.length < 2 || State.inbox;      // in the inbox: sorted in with the proposals instead
  select.replaceChildren(el('option', { value: '' }, t('Verschieben nach …')),
    ...targets.map((a) => el('option', { value: a.name }, a.name)));
  $('sel-take').hidden = !State.inbox || !takeTargets().length;
  fillTakeTargets($('sel-take'), t('Übernehmen & einsortieren nach …'));
  $('sel-propose').hidden = !State.inbox || !State.ai.enabled;
}

$('sel-move').addEventListener('change', async (event) => {
  const area = event.target.value;
  event.target.value = '';
  const n = State.selected.size;
  if (!area || !n) return;
  if (!window.confirm(n === 1 ? t('Das Bild in den Bereich {0} verschieben?', [area]) : t('{0} Bilder in den Bereich {1} verschieben?', [number(n), area]))) return;
  event.target.disabled = true;
  await runMany('move', { area }, (text, isBad) => message($('sel-msg'), text, isBad), t('nach {0} verschoben', [area]));
  event.target.disabled = false;
  State.areas = await api('areas');
  showInbox();
});

$('sel-sort').addEventListener('click', async () => {
  for (const id of ['sel-sort', 'sel-edit']) $(id).disabled = true;
  await runMany('sort', {}, (text, isBad) => message($('sel-msg'), text, isBad), t('einsortiert (oder schon am Platz)'));
  $('sel-sort').disabled = false;
});

function ruleRow(rule) {
  const row = el('div', { class: 'sort-rule' },
    el('input', { class: 'fd-input', value: rule.scope, list: 'sort-folders', placeholder: t('Familie'), 'aria-label': t('Gilt für Ordner') }),
    el('input', { class: 'fd-input', value: rule.folder, placeholder: t('bleibt'), spellcheck: 'false', 'aria-label': t('Ordner-Schema') }),
    el('input', { class: 'fd-input', value: rule.name, placeholder: t('bleibt'), spellcheck: 'false', 'aria-label': t('Namens-Schema') }),
    el('button', { class: 'fd-button fd-button--transparent', type: 'button', onclick: () => row.remove() }, t('Entfernen')));
  return row;
}

function showSorting(state) {
  $('sort-blocks').textContent = state.blocks.map((b) => `{${b}}`).join(' ');
  $('sort-rules').replaceChildren(
    el('div', { class: 'sort-rule sort-rule--head' }, el('span', {}, t('Gilt für Ordner')), el('span', {}, t('Ordner-Schema')), el('span', {}, t('Namens-Schema')), el('span')),
    el('datalist', { id: 'sort-folders' }, ...State.tree.map((n) => el('option', { value: n.path }))),
    ...state.rules.map(ruleRow));
  $('sort-modes').replaceChildren(...State.areas.flatMap((area) => {
    const select = el('select', { dataset: { area: area.name }, 'aria-label': t('Nach Datumsänderung in {0}', [area.name]) },
      el('option', { value: 'ask' }, t('nachfragen')), el('option', { value: 'auto' }, t('gleich einsortieren')));
    select.value = state.modes[area.name] || 'ask';
    return [el('span', {}, area.name), select];
  }));
  $('sort-all').disabled = $('sort-preview').disabled = !state.misfits;
  return state.misfits;
}
const misfitText = (n) => (n === 0 ? t('Alle Bilder liegen, wo sie hingehören.') : n === 1 ? t('1 Bild passt nicht zum Schema.') : t('{0} Bilder passen nicht zum Schema.', [number(n)]));

async function loadSorting() {
  const state = await api('sorting');
  message($('sort-msg'), state.rules.length ? misfitText(showSorting(state)) : (showSorting(state), t('Noch keine Regel – Augenblick! fasst keine Datei an.')));
  $('sort-moves').hidden = true;
}

$('sort-add').addEventListener('click', () => {
  const [folder, name] = ($('sort-preset').value || '|').split('|');
  const row = ruleRow({ scope: State.areas.length === 1 ? State.areas[0].name : '', folder, name });
  $('sort-rules').append(row);
  row.querySelector('input').focus();
});

$('sort-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const rules = [...$('sort-rules').querySelectorAll('.sort-rule:not(.sort-rule--head)')].map((row) => {
    const [scope, folder, name] = [...row.querySelectorAll('input')].map((input) => input.value);
    return { scope, folder, name };
  });
  const modes = Object.fromEntries([...$('sort-modes').querySelectorAll('select')].map((s) => [s.dataset.area, s.value]));
  try {
    const state = await api('sorting', { method: 'PUT', body: JSON.stringify({ rules, modes }) });
    message($('sort-msg'), t('Gespeichert. ') + misfitText(showSorting(state)));
    State.me = await api('me');
    $('misfit-box').hidden = $('sel-sort').hidden = !State.me.sorting;
    State.stale = true;
  } catch (err) { message($('sort-msg'), explain(err.message), true); }
});

$('sort-preview').addEventListener('click', async () => {
  const preview = await api('sort/preview?limit=300');
  const box = $('sort-moves');
  box.hidden = false;
  box.replaceChildren(
    el('p', { class: 'field-hint' }, preview.total > preview.moves.length
      ? t('Die ersten {0} von {1} – so würde einsortiert:', [preview.moves.length, number(preview.total)]) : t('So würde einsortiert:')),
    el('table', {}, el('tbody', {}, ...preview.moves.map((m) => el('tr', {}, el('td', {}, m.from), el('td', {}, '→'), el('td', {}, m.to))))));
});

$('sort-all').addEventListener('click', async (event) => {
  const start = await api('sort/preview?limit=1');
  if (!window.confirm(t('{0} Bilder einsortieren? Jedes Verschieben steht danach bei den Änderungen und lässt sich zurücknehmen.', [number(start.total)]))) return;
  event.target.disabled = true;
  const batch = randomHex();
  let moved = 0; let failed = 0; let reason = '';
  for (;;) {
    let step;
    try { step = await api('sort/all?batch=' + batch, { method: 'POST' }); }
    catch (err) { reason = explain(err.message); break; }
    moved += step.moved;
    failed = step.failed;
    if (step.errors.length) reason = explain(step.errors[0].error);
    message($('sort-msg'), t('{0} einsortiert, {1} noch offen …', [number(moved), number(step.left)]));
    if (!step.moved) break;
  }
  message($('sort-msg'), t('{0} Bilder einsortiert.', [number(moved)]) + (failed || reason ? ` ${failed ? number(failed) + ' nicht: ' : ''}${reason}` : ''), Boolean(failed || reason));
  State.stale = true;
  showSorting(await api('sorting'));
});

// ----------------------------------------------------------------- folder -> album

let jobTimer = 0;
async function watchJob() {
  clearTimeout(jobTimer);
  let job;
  try { job = await api('job'); } catch { return; }
  const person = job.kind === 'person' || job.kind === 'immich';
  if (job.running) {
    $('status').textContent = person ? t('Schreibt Namen ({0}): {1} von {2}', [job.what, number(job.done), number(job.total)]) : t('Schreibt Alben: {0} von {1}', [number(job.done), number(job.total)]);
    jobTimer = setTimeout(watchJob, person ? 1000 : 2000);
  } else if (job.kind) {
    $('status').textContent = (person ? t('Namen geschrieben ({0}): {1} von {2}', [job.what, number(job.done - job.failed), number(job.total)]) : t('Alben geschrieben: {0} von {1}', [number(job.done - job.failed), number(job.total)])) + (job.failed ? t(', {0} nicht ({1})', [number(job.failed), explain(job.errors[0].error)]) : '');
    if (job.kind === 'immich' && $('immich-msg')) message($('immich-msg'), $('status').textContent, job.failed > 0);
    if (person) { State.stale = true; await loadPeople(); reload(true); } else loadAlbums();
  }
}

/* Another name for somebody in every photo; a name that exists already merges the two. */
$('btn-rename-person').addEventListener('click', () => renamePerson(State.person));
async function renamePerson(name) {
  const to = (window.prompt(`Neuer Name für „${name}“ in allen Bildern.\n\nGibt es den Namen schon, werden beide zu einer Person.`, name) || '').trim();
  if (!to || to === name) return;
  const other = State.people.find((p) => p.name === to);
  const mine = State.people.find((p) => p.name === name);
  const count = mine ? tn(mine.count, '1 Bild', '{0} Bilder') : t('alle Bilder');
  if (!window.confirm(other
    ? t('„{0}“ und „{1}“ zusammenführen?\n\nIn {2} wird aus „{3}“ der Name „{4}“. Jedes Bild steht danach bei den Änderungen und lässt sich einzeln zurücknehmen.', [name, to, count, name, to])
    : t('„{0}“ in {1} in „{2}“ umbenennen?\n\nJedes Bild steht danach bei den Änderungen und lässt sich einzeln zurücknehmen.', [name, count, to]))) return;
  try {
    const start = await api('people/rename', { method: 'POST', body: JSON.stringify({ name, to }) });
    if (start.skipped) $('status').textContent = t('{0} Bilder liegen in Bereichen ohne Schreibrecht und bleiben, wie sie sind.', [number(start.skipped)]);
    location.hash = personHref(to);
    watchJob();
  } catch (err) { $('status').textContent = explain(err.message); }
}

$('btn-to-album').addEventListener('click', () => folderToAlbum(State.folder));
async function folderToAlbum(folder) {
  const album = folder.split('/').slice(1).join('/');
  if (!window.confirm(t('In jedes Bild unter „{0}“ schreiben, in welchem Ordner es liegt?\n\nBilder direkt in diesem Ordner bekommen das Album „{1}“, Bilder in Unterordnern das passende Unteralbum. Die Dateien bleiben, wo sie sind. Bei vielen Bildern dauert das; der Fortschritt steht oben rechts.', [folder, album]))) return;
  try {
    await api('albums/from-folder', { method: 'POST', body: JSON.stringify({ folder }) });
    watchJob();
  } catch (err) { $('status').textContent = explain(err.message); }
}

// ----------------------------------------------------------------- what runs in the background

let jobsTimer = 0;
const JOB_NAMES = { album: t('Ordner als Album merken'), immich: t('Personen aus Immich übernehmen'), person: t('Person umbenennen'), dupes: t('Kopien auflösen'), aicheck: t('KI prüft Gesichter') };
const ago = (ts) => (ts ? new Date(ts * 1000).toLocaleString(LOCALE, { dateStyle: 'short', timeStyle: 'short' }) : t('noch nie'));
function bar(id, done, total, show) {
  const node = $(id);
  node.hidden = !show;
  node.max = Math.max(1, total);
  node.value = Math.min(done, Math.max(1, total));
}

async function showJobs() {
  clearTimeout(jobsTimer);
  if ($('view-settings').hidden || document.querySelector('.set-pane[data-pane="auftraege"]').hidden) return;
  let all;
  try { all = await api('jobs'); } catch { return; }
  const { scan, faces, job, dupes } = all;

  $('job-scan-text').textContent = scan.running && scan.total
    ? t('Liest gerade ein: {0} von {1} neuen oder geänderten Dateien.', [number(scan.done), number(scan.total)])
    : t('Ruht. {0} Bilder und Videos im Index, zuletzt nachgesehen {1}. Von selbst alle {2} Minuten.', [number(scan.photos), ago(scan.last), scan.every_minutes]);
  bar('job-scan-bar', scan.done, scan.total, scan.running && scan.total > 0);

  const left = Math.max(0, faces.wanted - faces.pictures);
  const seen = t('{0} von {1} Bildern durchgesehen, {2} Gesichter gefunden.', [number(Math.min(faces.pictures, faces.wanted)), number(faces.wanted), number(faces.faces)]);
  $('job-faces-text').textContent = faces.missing ? t('Nicht eingerichtet: Die Modelle fehlen auf diesem Rechner.')
    : !faces.enabled ? t('Ausgeschaltet. Einschalten unter Einstellungen → Personen.')
    : faces.search.running ? t('Sucht gerade. {0}', [seen])
    : left ? t('Wartet. {0} {1} stehen noch aus – die Suche macht von selbst weiter, sobald das Einlesen ruht.', [seen, number(left)])
    : t('Fertig. {0} Zuletzt gelaufen {1}.', [seen, ago(faces.search.last)]);
  bar('job-faces-bar', faces.pictures, faces.wanted, faces.enabled && !faces.missing && left > 0);
  $('job-faces').disabled = !faces.enabled || Boolean(faces.missing) || faces.search.running;

  const open = Math.max(0, dupes.wanted - dupes.pictures);
  $('job-dupes-text').textContent = dupes.search.running
    ? t('Sieht sich die Bilder an: {0} von {1}.', [number(dupes.search.done), number(dupes.search.total)])
    : !dupes.pictures ? t('Noch nie gelaufen.')
    : t('{0} von {1} Bildern angesehen', [number(Math.min(dupes.pictures, dupes.wanted)), number(dupes.wanted)]) + (open ? t(', {0} sind seither dazugekommen.', [number(open)]) : '.')
      + (dupes.search.last ? t(' Zuletzt gelaufen {0}.', [ago(dupes.search.last)]) : '');
  bar('job-dupes-bar', dupes.search.done, dupes.search.total, dupes.search.running);
  $('job-dupes').disabled = dupes.search.running;

  const name = JOB_NAMES[job.kind] || job.kind;
  $('job-long-text').textContent = job.running
    ? t('{0} ({1}): {2} von {3}', [name, jobWhat(job.what), number(job.done), number(job.total)]) + (job.failed ? t(', {0} nicht geschrieben', [number(job.failed)]) : '') + (job.stop ? t(' – wird abgebrochen …') : '')
    : job.kind
      ? t('Zuletzt: {0} ({1}), beendet {2} – {3} von {4} bearbeitet', [name, jobWhat(job.what), ago(job.ended), number(job.done), number(job.total)]) + (job.failed ? t(', {0} nicht geschrieben', [number(job.failed)]) + '.' : '.') + (job.done < job.total ? t(' Abgebrochen.') : '')
      : t('Seit dem letzten Start von Augenblick! lief keiner.');
  bar('job-long-bar', job.done, job.total, job.running);
  $('job-stop').hidden = !job.running || !State.me.is_admin;
  $('job-long-errors').replaceChildren(...job.errors.slice(0, 10).map((e) => el('li', {}, `${e.path}: ${explain(e.error)}`)));

  jobsTimer = setTimeout(showJobs, scan.running || faces.search.running || job.running || dupes.search.running ? 3000 : 10000);
}

async function jobAction(path, done) {
  try { await api(path, { method: 'POST' }); message($('job-msg'), done); }
  catch (err) { message($('job-msg'), explain(err.message), true); }
  setTimeout(showJobs, 1200);
}
$('job-scan').addEventListener('click', () => jobAction('rescan', t('Einlesen angestoßen.')));
$('job-faces').addEventListener('click', () => jobAction('faces/search', t('Suche angestoßen.')));
$('job-dupes').addEventListener('click', () => jobAction('dupes/search', t('Duplikatsuche angestoßen.')));
$('job-stop').addEventListener('click', () => {
  if (window.confirm(t('Den laufenden Auftrag abbrechen? Was schon geschrieben wurde, bleibt.'))) jobAction('job/stop', t('Wird abgebrochen.'));
});

// ----------------------------------------------------------------- AI

async function loadAiOffer() {
  try { State.ai = await api('ai/prompts'); } catch { return; }
  const select = $('v-ai-prompt');
  select.replaceChildren(...State.ai.prompts.map((name) => el('option', { value: name }, name)));
  select.value = State.ai.default;
  select.hidden = State.ai.prompts.length < 2;
  if (State.me) fillMoveTargets();
}

const cents = (usd) => (usd > 0 ? `, ${(usd * 100).toLocaleString(LOCALE, { maximumFractionDigits: 2 })} ct` : '');

$('v-ai').addEventListener('click', async () => {
  const photo = Viewer.photo;
  $('v-ai').disabled = true;
  message($('v-msg'), t('Die KI sieht sich das Bild an …'));
  try {
    const idea = await api(`photos/${photo.key}/ai`, { method: 'POST', body: JSON.stringify({ prompt: $('v-ai-prompt').value || null }) });
    if (Viewer.key !== photo.key) return;
    if (photo.inbox && !$('v-inbox').hidden) {      // kept as the proposal; chosen from when sorting it in
      photo.proposal = idea;
      showTake(photo);
      message($('v-msg'), t('Vorschlag der KI ({0}{1}). Wähle aus, was ins Bild soll, dann „Übernehmen & einsortieren“.', [idea.model, cents(idea.cost)]));
      return;
    }
    $('v-edit').click();
    if (idea.description) $('e-desc').value = idea.description;
    if (idea.keywords.length) $('e-words').value = [...new Set([...photo.keywords, ...idea.keywords])].join(', ');
    let dated = '';
    if (idea.date && photo.taken_src !== 'exif') {
      $('e-taken').value = idea.date.taken;
      dated = { year: t(' Datum geschätzt – gemeint ist nur das Jahr.'), month: t(' Datum geschätzt – gemeint ist nur der Monat.'), day: t(' Datum geschätzt.') }[idea.date.precision];
    }
    message($('v-msg'), t('Vorschlag der KI ({0}{1}). Prüfen, dann „Ins Bild schreiben“.{2}{3}', [idea.model, cents(idea.cost), dated, idea.note ? ' ' + idea.note : '']));
  } catch (err) { message($('v-msg'), explain(err.message), true); }
  finally { $('v-ai').disabled = false; }
});

function promptRow(prompt, isDefault) {
  const row = el('div', { class: 'ai-prompt' },
    el('label', { class: 'check', title: t('vorausgewählt') }, el('input', { type: 'radio', name: 'ai-default', checked: isDefault })),
    el('input', { class: 'fd-input', value: prompt.name, maxlength: 60, placeholder: t('Name'), 'aria-label': t('Name der Anweisung') }),
    el('button', { class: 'fd-button fd-button--transparent', type: 'button', onclick: () => row.remove() }, t('Entfernen')),
    el('textarea', { class: 'fd-textarea', rows: 4, maxlength: 6000, 'aria-label': t('Text der Anweisung') }));
  row.querySelector('textarea').value = prompt.text;
  return row;
}

function showAI(config) {
  $('ai-enabled').checked = config.enabled;
  $('ai-memories').checked = Boolean(config.memories);
  $('ai-inbox-auto').checked = Boolean(config.inbox_auto);
  $('ai-provider').value = config.provider;
  $('ai-url').value = config.base_url;
  $('ai-key').value = '';
  $('ai-key').placeholder = config.key_set ? t('gespeichert ({0}) – leer lassen = unverändert', [config.key_hint || '…']) : (config.provider === 'anthropic' ? 'sk-ant-…' : t('nur falls der Server einen verlangt'));
  $('ai-clear-key').hidden = !config.key_set;
  $('ai-limit').value = config.monthly_limit_usd;
  const used = config.month_usage;
  $('ai-usage').textContent = t('Diesen Monat: {0} Anfragen, {1}', [used.calls, used.cost.toLocaleString(LOCALE, { style: 'currency', currency: 'USD' })]) + (config.monthly_limit_usd ? '' : t(' – kein Limit gesetzt'));
  fillModels(config.models, config.model);
  $('ai-areas').replaceChildren(...State.areas.map((area) => el('label', { class: 'check' },
    el('input', { type: 'checkbox', value: area.name, checked: config.excluded_areas.includes(area.name) }), el('span', {}, area.name))));
  $('ai-prompts').replaceChildren(...config.prompts.map((p) => promptRow(p, p.name === config.default_prompt)));
  syncAiProvider();
}
function syncAiProvider() {
  const own = $('ai-provider').value !== 'anthropic';
  document.querySelectorAll('.ai-url').forEach((node) => { node.hidden = !own; });
}
$('ai-provider').addEventListener('change', syncAiProvider);
async function loadAI() { showAI(await api('ai')); message($('ai-msg'), ''); }

async function saveAI(extra = {}) {
  const rows = [...$('ai-prompts').children];
  const prompts = rows.map((row) => ({ name: row.querySelector('input.fd-input').value, text: row.querySelector('textarea').value }));
  const chosen = rows.find((row) => row.querySelector('input[type=radio]').checked);
  const body = {
    enabled: $('ai-enabled').checked, memories: $('ai-memories').checked, inbox_auto: $('ai-inbox-auto').checked, provider: $('ai-provider').value, model: $('ai-model').value.trim(),
    base_url: $('ai-url').value.trim(), monthly_limit_usd: Number($('ai-limit').value) || 0,
    excluded_areas: [...$('ai-areas').querySelectorAll('input:checked')].map((box) => box.value),
    prompts, default_prompt: chosen ? chosen.querySelector('input.fd-input').value.trim() : '', ...extra,
  };
  if ($('ai-key').value.trim()) body.api_key = $('ai-key').value.trim();
  showAI(await api('ai', { method: 'PUT', body: JSON.stringify(body) }));
  loadAiOffer();
}
$('ai-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await saveAI(); message($('ai-msg'), t('Gespeichert.')); }
  catch (err) { message($('ai-msg'), explain(err.message), true); }
});
$('ai-clear-key').addEventListener('click', async () => {
  try { await saveAI({ clear_key: true }); message($('ai-msg'), t('Schlüssel entfernt.')); }
  catch (err) { message($('ai-msg'), explain(err.message), true); }
});
$('ai-add-prompt').addEventListener('click', () => {
  const row = promptRow({ name: '', text: '' }, !$('ai-prompts').children.length);
  $('ai-prompts').append(row);
  row.querySelector('input.fd-input').focus();
});
function fillModels(models, current) {
  // the model in use stays selectable even when the list does not name it
  const all = current && !models.some((m) => m.id === current) ? [{ id: current, label: current }, ...models] : models;
  $('ai-model').replaceChildren(...all.map((m) => el('option', { value: m.id }, m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id)));
  $('ai-model').value = current || (all[0] ? all[0].id : '');
}
$('ai-load-models').addEventListener('click', async () => {
  try {
    await saveAI();
    const found = await api('ai/models');
    fillModels(found.models, $('ai-model').value);
    message($('ai-msg'), found.error ? explain(found.error) : t('{0} Modelle gefunden – in der Liste „Modell“ auswählbar.', [found.models.length]), Boolean(found.error));
  } catch (err) { message($('ai-msg'), explain(err.message), true); }
});
$('ai-test').addEventListener('click', async () => {
  message($('ai-msg'), t('Fragt die KI …'));
  try {
    await saveAI();
    const answer = await api('ai/test', { method: 'POST' });
    message($('ai-msg'), t('Die KI antwortet: „{0}“ ({1}, {2} s{3}).', [answer.answer, answer.model, answer.seconds.toLocaleString(LOCALE), cents(answer.cost)]));
  } catch (err) { message($('ai-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- people in the viewer

/* The boxes lie over the picture exactly where it is drawn, whatever room the page leaves it. */
function placeFaces() {
  const box = $('v-faces'); const img = $('v-img');
  if (box.hidden || img.hidden) return;
  const stage = img.parentElement.getBoundingClientRect(); const at = img.getBoundingClientRect();
  Object.assign(box.style, { left: `${at.left - stage.left}px`, top: `${at.top - stage.top}px`, width: `${at.width}px`, height: `${at.height}px` });
}
window.addEventListener('resize', placeFaces);
$('v-img').addEventListener('load', placeFaces);

function hotFace(at, on) {
  for (const node of document.querySelectorAll(`[data-face="${at}"]`)) node.classList.toggle('is-hot', on);
}

async function loadFaces(photo) {
  let faces;
  try { faces = await api(`photos/${photo.key}/faces`, { quiet: true }); } catch { return; }
  if (Viewer.key !== photo.key || Viewer.photo !== photo) return;
  Viewer.faces = faces;
  stopDrawing();
  showFaces(photo);
}

/* What is known about the faces of the shown photo: the line among the facts, the boxes, the list. */
function showFaces(photo) {
  const all = Viewer.faces;
  const faces = all.filter((face) => !face.ignored); const away = all.filter((face) => face.ignored);
  const label = $('v-people-label'); const fact = $('v-people-fact');
  if (!label || (!all.length && !photo.people.length && !photo.can_write)) return;
  label.hidden = fact.hidden = false;
  const open = store.get('faces.show', false) || faces.some((face) => face.drawn);
  fact.replaceChildren(
    ...photo.people.map((name) => el('a', { class: 'tag', href: personHref(name), onclick: closeViewer }, name)),
    all.length || photo.can_write ? el('button', { class: 'fd-button fd-button--compact', type: 'button', 'aria-pressed': String(open),
      onclick: () => { store.set('faces.show', !open); Viewer.faces = Viewer.faces.filter((face) => !face.drawn); stopDrawing(); showFaces(photo); } },
    open ? t('Gesichter ausblenden') : t('Gesichter zeigen ({0})', [faces.length])) : null);
  const boxes = $('v-faces'); const list = $('v-people');
  boxes.hidden = list.hidden = !open;
  if (boxes.hidden) return;
  boxes.replaceChildren(...faces.map((face, at) => el('button', {
    class: 'face-box' + (face.name ? ' is-named' : '') + (face.drawn ? ' is-drawn' : ''), type: 'button', dataset: { face: at },
    style: `left:${(face.x - face.w / 2) * 100}%;top:${(face.y - face.h / 2) * 100}%;width:${face.w * 100}%;height:${face.h * 100}%`,
    'aria-label': face.name || t('Gesicht ohne Namen'),
    onmouseenter: () => hotFace(at, true), onmouseleave: () => hotFace(at, false),
    onclick: () => { const input = list.querySelector(`[data-face="${at}"] input`); if (input) input.focus(); },
  }, el('span', { class: face.name ? '' : 'is-guess' }, face.name || (face.proposal ? face.proposal + '?' : '')))));
  const drawing = boxes.classList.contains('is-drawing');
  const bar = el('div', { class: 'people-edit__bar' }, el('h4', {}, faces.length ? t('Gesichter im Bild') : t('Kein Gesicht gefunden')),
    photo.can_write ? el('button', { class: 'fd-button fd-button--compact', type: 'button', 'aria-pressed': String(drawing),
      title: t('Einen Rahmen um ein Gesicht ziehen, das nicht gefunden wurde'),
      onclick: () => { if (drawing) stopDrawing(); else startDrawing(); showFaces(photo); } },
    drawing ? t('Abbrechen') : t('Gesicht einzeichnen')) : null,
    drawing ? el('span', { class: 'field-hint' }, t('Zieh im Bild einen Rahmen um das Gesicht.')) : null);
  list.replaceChildren(bar, ...faces.map((face, at) => {
    const picture = face.face ? el('img', { src: `api/faces/${face.face}/thumb`, alt: '', loading: 'lazy' }) : el('span', { class: 'person-none' });
    const row = el('form', { class: 'person-row', dataset: { face: at },
      onmouseenter: () => hotFace(at, true), onmouseleave: () => hotFace(at, false) }, picture);
    if (!photo.can_write) { row.append(el('span', {}, face.name || t('ohne Namen'))); return row; }
    const input = el('input', { class: 'fd-input', list: 'people-names', autocomplete: 'off', maxlength: 100,
      value: face.name || face.proposal, placeholder: t('Name'), 'aria-label': t('Name für dieses Gesicht'),
      onfocus: () => hotFace(at, true), onblur: () => hotFace(at, false) });
    row.append(input, el('button', { class: 'fd-button fd-button--compact' + (face.name ? '' : ' fd-button--emphasized'),
      type: 'submit', title: t('Namen ins Bild schreiben') }, face.name ? t('Ändern') : t('Benennen')));
    if (face.marked) {
      row.append(el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
        title: t('Markierung aus dem Bild entfernen'), onclick: () => nameFace(photo, face, '') }, t('Entfernen')));
      if (face.name && face.face) {
        row.append(el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
          title: t('Dieses Gesicht in der Personenliste für {0} zeigen', [face.name]),
          onclick: async (event) => {
            const button = event.currentTarget;
            try { await api('people/cover', { method: 'PUT', body: JSON.stringify({ name: face.name, face: face.face }) }); }
            catch (err) { button.textContent = explain(err.message); return; }
            button.textContent = t('✓ Schlüsselbild');
            loadPeople();
          } }, t('Als Schlüsselbild')));
      }
    } else if (face.drawn) {
      row.append(el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
        onclick: () => { Viewer.faces = Viewer.faces.filter((other) => other !== face); showFaces(photo); } }, t('Doch nicht')));
    } else if (face.face) {
      row.append(el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
        title: t('Niemand, der benannt werden soll: nicht mehr vorschlagen. Am Bild ändert das nichts.'),
        onclick: () => ignoreFaces(photo, [face.face], true) }, t('Ignorieren')));
    }
    row.addEventListener('submit', (event) => {
      event.preventDefault();
      if (input.value.trim()) nameFace(photo, face, input.value.trim());
    });
    return row;
  }), ...(away.length ? [el('h4', {}, t('Ignoriert'))] : []), ...away.map((face) => el('div', { class: 'person-row is-away' },
    el('img', { src: `api/faces/${face.face}/thumb`, alt: '', loading: 'lazy' }), el('span', {}, t('nicht mehr vorgeschlagen')),
    photo.can_write ? el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
      onclick: () => ignoreFaces(photo, [face.face], false) }, t('Zurückholen')) : null)));
  placeFaces();
  const fresh = faces.findIndex((face) => face.drawn);
  if (fresh >= 0) { const input = list.querySelector(`[data-face="${fresh}"] input`); if (input) input.focus(); }
}

async function ignoreFaces(photo, ids, ignored) {
  try {
    await api('faces/ignore', { method: 'POST', body: JSON.stringify({ faces: ids, ignored }) });
    State.stale = true;
    if (Viewer.key === photo.key) loadFaces(photo);
  } catch (err) { message($('v-msg'), explain(err.message), true); }
}

/* Drawing a face by hand: drag a box over the picture, then give it a name like any other face. */
const Draw = { from: null, draft: null };
function startDrawing() { $('v-faces').classList.add('is-drawing'); }
function stopDrawing() {
  $('v-faces').classList.remove('is-drawing');
  if (Draw.draft) Draw.draft.remove();
  Draw.from = Draw.draft = null;
}
function drawSpot(event) {
  const at = $('v-faces').getBoundingClientRect();
  return [Math.min(1, Math.max(0, (event.clientX - at.left) / at.width)), Math.min(1, Math.max(0, (event.clientY - at.top) / at.height))];
}
$('v-faces').addEventListener('pointerdown', (event) => {
  const boxes = $('v-faces');
  if (!boxes.classList.contains('is-drawing') || event.button) return;
  event.preventDefault();
  boxes.setPointerCapture(event.pointerId);
  Draw.from = drawSpot(event);
  Draw.draft = boxes.appendChild(el('div', { class: 'face-draft' }));
});
$('v-faces').addEventListener('pointermove', (event) => {
  if (!Draw.from) return;
  const [x, y] = drawSpot(event); const [fx, fy] = Draw.from;
  Object.assign(Draw.draft.style, { left: `${Math.min(x, fx) * 100}%`, top: `${Math.min(y, fy) * 100}%`,
    width: `${Math.abs(x - fx) * 100}%`, height: `${Math.abs(y - fy) * 100}%` });
});
$('v-faces').addEventListener('pointerup', (event) => {
  if (!Draw.from) return;
  const [x, y] = drawSpot(event); const [fx, fy] = Draw.from;
  const at = $('v-faces').getBoundingClientRect();
  const w = Math.abs(x - fx); const h = Math.abs(y - fy);
  stopDrawing();
  if (w * at.width >= 12 && h * at.height >= 12 && Viewer.photo) {      // a mere click draws nothing
    Viewer.faces = Viewer.faces.filter((face) => !face.drawn).concat({ x: (x + fx) / 2, y: (y + fy) / 2, w, h,
      name: '', marked: false, face: null, proposal: '', ignored: false, drawn: true });
  }
  if (Viewer.photo) showFaces(Viewer.photo);
});
$('v-faces').addEventListener('pointercancel', () => { stopDrawing(); if (Viewer.photo) showFaces(Viewer.photo); });

async function nameFace(photo, face, name) {
  message($('v-msg'), t('Schreibt …'));
  try {
    const updated = await api(`photos/${photo.key}/faces`, { method: 'PUT',
      body: JSON.stringify({ x: face.x, y: face.y, w: face.w, h: face.h, name }) });
    adopt(updated, photo.key);
    loadPeople();
    if (Viewer.key === photo.key) {
      Viewer.key = updated.key;
      showPhoto(updated);
      message($('v-msg'), name ? t('{0} steht jetzt im Bild.', [name]) : t('Markierung entfernt.'));
    }
  } catch (err) { message($('v-msg'), explain(err.message), true); }
}

// ----------------------------------------------------------------- pictures that are there twice

const Dupes = { shown: 20, timer: 0 };

function fileSize(bytes) {
  return bytes > 1048576 ? `${(bytes / 1048576).toLocaleString(LOCALE, { maximumFractionDigits: 1 })} MB` : `${Math.round(bytes / 1024)} KB`;
}

async function renderDupes() {
  if (!State.me) return;
  clearTimeout(Dupes.timer);
  const level = $('dupes-level').value;
  let found; let job = { running: false };
  try {
    found = await api(`dupes?limit=${Dupes.shown}&level=${level}&areas=${encodeURIComponent(areaQuery())}`);
    job = (await api('jobs', { quiet: true })).job;
  } catch { return; }
  const running = found.search.running;
  const busy = job.running && job.kind === 'dupes';
  $('dupes-line').textContent = (running ? t('Die Suche läuft: {0} von {1} Bildern angesehen. ', [number(found.search.done), number(found.search.total)]) : '')
    + (busy ? t('Löst gerade alle Kopien auf: {0} von {1}. ', [number(job.done), number(job.total)]) : '')
    + (!found.pictures && !running ? t('Die Suche ist noch nie gelaufen – bisher stehen hier nur Dateien mit genau gleichem Inhalt. ') : '')
    + (found.total ? t('{0} mehrfach da; {1} überzählig.', [found.total === 1 ? t('1 Bild ist') : t('{0} Bilder sind', [number(found.total)]), found.spare === 1 ? t('1 Datei wäre') : t('{0} Dateien wären', [number(found.spare)])]) : t('Kein Bild ist mehrfach da.'));
  $('dupes-search').disabled = running;
  $('dupes-all').hidden = level !== 'copies' || !found.sure || !State.me.is_admin;
  $('dupes-all').disabled = busy;
  $('dupes-all').dataset.count = found.sure; $('dupes-all').dataset.spare = found.sure_spare;
  $('dupes-all').textContent = t('Eindeutige Kopien in den Papierkorb ({0} Dateien)', [number(found.sure_spare)]);
  $('dupes-all').title = t('Bei allen Bildern, die eindeutig mehrfach da sind (gleiche Bilddaten oder dasselbe Bild in anderer Größe), die größte Fassung behalten und die übrigen in den Papierkorb legen – mit Rückfrage');
  $('dupes-more').hidden = found.total <= found.groups.length;
  $('dupe-groups').replaceChildren(...found.groups.map(dupeGroup));
  if ((running || busy) && !$('view-dupes').hidden) Dupes.timer = setTimeout(renderDupes, 5000);
}
$('dupes-more').addEventListener('click', () => { Dupes.shown += 20; renderDupes(); });
$('dupes-level').addEventListener('change', () => { Dupes.shown = 20; renderDupes(); });
$('dupes-all').addEventListener('click', async (event) => {
  const { count, spare } = event.currentTarget.dataset;
  const merge = $('dupes-merge').checked;
  if (!window.confirm(t('Bei {0} Bildern ist die Sache eindeutig: dieselben Bilddaten oder dasselbe Bild in anderer Größe. Dort die größte Fassung behalten und die übrigen {1} Dateien in den Papierkorb legen?\n\nGruppen mit gleich großen Bildern bleiben stehen – das können zwei Aufnahmen vom selben Platz sein.\n', [number(Number(count)), number(Number(spare))])
    + (merge ? t('Alben, Schlagworte, Namen und weitere Angaben der anderen Fassungen werden vorher übernommen.') : t('Angaben der anderen Fassungen werden NICHT übernommen.'))
    + t('\nDer Papierkorb hebt die Dateien für die eingestellte Frist auf.'))) return;
  try {
    const done = await api('dupes/resolve-all', { method: 'POST', body: JSON.stringify({ merge }) });
    message($('dupes-msg'), t('Läuft: {0} Bilder. Fortschritt auch unter Einstellungen → Aufträge, dort lässt es sich abbrechen.', [number(done.started)]));
  } catch (err) { message($('dupes-msg'), explain(err.message), true); }
  setTimeout(renderDupes, 1500);
});
$('dupes-search').addEventListener('click', async () => {
  try { await api('dupes/search', { method: 'POST' }); message($('dupes-msg'), t('Suche angestoßen.')); }
  catch (err) { message($('dupes-msg'), explain(err.message), true); }
  setTimeout(renderDupes, 1500);
});

function dupeGroup(group) {
  const name = 'dupe-' + group.id;
  const may = group.files.every((file) => file.can_delete);
  const cards = group.files.map((file, at) => {
    const facts = [`${file.width} × ${file.height}, ${fileSize(file.size)}`,
      file.taken ? new Date(file.taken).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' }) + (file.taken_src === 'exif' ? '' : t(' (nicht aus dem Bild)')) : '',
      file.albums.length ? t('Alben: {0}', [file.albums.join(', ')]) : '', file.people.length ? t('Personen: {0}', [file.people.join(', ')]) : '',
      file.keywords.length ? t('Schlagworte: {0}', [file.keywords.join(', ')]) : '', file.rating ? '★'.repeat(file.rating) : ''].filter(Boolean);
    return el('div', { class: 'dupe-file', oncontextmenu: (event) => {
      event.preventDefault();
      const radio = event.currentTarget.querySelector('input[type="radio"]');
      const others = group.files.length - 1;
      showMenu(event.clientX, event.clientY, [
        [t('Bild öffnen'), () => openViewer(file.key, null)],
        [t('Ordner öffnen'), () => { location.hash = '#/ordner/' + file.path.split('/').slice(0, -1).map(encodeURIComponent).join('/'); }],
        may ? '-' : null,
        may ? [t('Diese behalten'), () => { radio.checked = true; }] : null,
        may ? [others === 1 ? t('Diese behalten, die andere in den Papierkorb') : t('Diese behalten, die {0} anderen in den Papierkorb', [others]),
          () => { radio.checked = true; remove.click(); }] : null,
        may && apart ? [t('Keine Duplikate'), () => apart.click()] : null,
      ]);
    } },
      el('button', { class: 'dupe-file__pic', type: 'button', title: t('Bild öffnen'), onclick: () => openViewer(file.key, null) },
        el('img', { src: thumbUrl({ key: file.key, o: file.orientation }), alt: '', loading: 'lazy' })),
      el('label', { class: 'check dupe-file__keep' }, el('input', { type: 'radio', name, value: file.key, checked: at === 0, disabled: !may }),
        el('span', {}, t('behalten'))),
      el('div', { class: 'dupe-file__path' }, file.path),
      ...facts.map((fact) => el('div', { class: 'field-hint' }, fact)));
  });
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const box = el('div', { class: 'face-group dupe-group' });
  const act = async (button, work) => {
    button.disabled = true;
    try { await work(); } catch (err) { message(msg, explain(err.message), true); button.disabled = false; return; }
    State.stale = true;
    await renderDupes();
  };
  const remove = el('button', { class: 'fd-button fd-button--emphasized', type: 'button' },
    group.files.length === 2 ? t('Die andere in den Papierkorb') : t('Die {0} anderen in den Papierkorb', [group.files.length - 1]));
  remove.addEventListener('click', () => act(remove, async () => {
    const keep = box.querySelector('input[type="radio"]:checked').value;
    const done = await api('dupes/resolve', { method: 'POST', body: JSON.stringify({ keep,
      drop: group.files.map((file) => file.key).filter((key) => key !== keep), merge: $('dupes-merge').checked }) });
    if (done.failed.length) throw new Error(done.failed[0].error);
    message($('dupes-msg'), t('{0} in den Papierkorb gelegt', [done.removed.length === 1 ? t('1 Datei') : t('{0} Dateien', [number(done.removed.length)])])
      + (done.merged.length ? t(', Angaben übernommen.') : '.'));
  }));
  const apart = group.exact ? null : el('button', { class: 'fd-button fd-button--transparent', type: 'button',
    title: t('Diese Bilder sehen sich nur ähnlich – nicht mehr als Duplikate zeigen') }, t('Keine Duplikate'));
  if (apart) apart.addEventListener('click', () => act(apart, () => api('dupes/apart', { method: 'POST', body: JSON.stringify({ id: group.id, level: $('dupes-level').value }) })));
  box.append(el('div', { class: 'dupe-files' }, ...cards),
    el('div', { class: 'face-group__form' }, el('strong', {}, group.exact ? t('Genau dieselben Bilddaten') : group.distance > 0.025 ? t('Sehr ähnliche Aufnahmen') : group.sure ? t('Dasselbe Bild, anders gespeichert') : t('Bitte ansehen: gleich große Bilder – Kopie oder zwei Aufnahmen vom selben Platz?')),
      may ? remove : el('span', { class: 'field-hint' }, t('Hier darfst du nicht löschen.')), may ? apart : null, msg));
  return box;
}

// ----------------------------------------------------------------- faces without a name

const FACES = 120;
const Faces = { shown: 0, state: null, timer: 0, open: null, faces: FACES, busy: new Set(), entries: [] };
const LAZY = '&stale=600';       // while names are being written an answer from a moment ago will do
const GROUPS = 60;

async function loadFaceSearch() {
  try { Faces.state = await api('faces', { quiet: true }); } catch { return null; }
  $('faces-link').hidden = !(Faces.state.enabled || Faces.state.pictures);

  showPeopleBox();
  return Faces.state;
}

function searchText(state) {
  if (state.missing) return t('Die Suche nach Gesichtern ist auf diesem Rechner nicht eingerichtet (die Modelle fehlen).');
  const done = t('{0} Bilder durchgesehen, {1} Gesichter gefunden.', [number(state.pictures), number(state.faces)]);
  if (!state.enabled) return t('Die Suche nach Gesichtern ist ausgeschaltet. {0}', [state.pictures ? done : '']);
  if (state.search.running) return t('Sucht gerade: {0} von {1} Bildern. Bisher {2}', [number(state.search.done), number(state.search.total), done]);
  return done;
}

async function renderFaceGroups(more = false, quiet = false) {
  if (!State.me) return;
  clearTimeout(Faces.timer);
  const state = await loadFaceSearch();
  if (!state) return;
  const limit = more ? Faces.shown + GROUPS : Math.max(GROUPS, Faces.shown);
  let found;
  try { found = await api(`faces/groups?limit=${limit}&shown=0&areas=${encodeURIComponent(areaQuery())}`); } catch { return; }
  Faces.shown = limit;
  $('faces-line').textContent = searchText(state) + (found.faces
    ? t(' {0} Gesichter ohne Namen in {1} Gruppen.', [number(found.faces), number(found.total)]) : state.pictures ? t(' Kein Gesicht ohne Namen.') : '')
    + (found.small ? t(' {0} weitere sind zu klein zum Vergleichen; sie lassen sich im einzelnen Bild benennen.', [number(found.small)]) : '');
  $('faces-more').hidden = found.total <= found.groups.length;
  // stay with the bundle that was open: the same one, else the same person, else the same place in the list
  let twins = [];
  try { twins = (await api(`faces/twins?areas=${encodeURIComponent(areaQuery())}`, { quiet: true })).faces; } catch { /* the page works without */ }
  Faces.twins = twins;
  let doubles = [];
  try { doubles = (await api(`faces/doubles?areas=${encodeURIComponent(areaQuery())}`, { quiet: true })).faces; } catch { /* the page works without */ }
  Faces.doubles = doubles;
  let none = { total: 0, waiting: 0, enabled: false }; let job = { running: false };
  try { none = await api('faces/no-face?limit=1', { quiet: true }); job = (await api('jobs', { quiet: true })).job; } catch { /* the page works without */ }
  const asking = job.running && job.kind === 'aicheck';
  $('faces-ai').hidden = !State.me.is_admin || !none.enabled || (!none.waiting && !asking);
  $('faces-ai').disabled = asking;
  $('faces-ai').dataset.waiting = none.waiting;
  $('faces-ai').textContent = asking ? t('KI prüft … {0} von {1} Anfragen', [number(job.done), number(job.total)]) : t('KI: Fehltreffer suchen ({0})', [number(none.waiting)]);
  let was = Faces.open;
  let rest = null;        // faces left over from the bundle just dealt with: stay with them, wherever they went
  if (was && was.keep) {
    try { rest = await api(`faces/groups/${was.keep}?limit=1&areas=${encodeURIComponent(areaQuery())}`, { quiet: true }); } catch { /* gone */ }
    was = rest ? { ...was, id: rest.id, count: -1 } : was;
  }
  const list = [...(doubles.length ? [{ id: 'doubles', count: doubles.length, proposal: '', known: false, thumb: doubles[0].id }] : []),
    ...(twins.length ? [{ id: 'twins', count: twins.length, proposal: '', known: false, thumb: twins[0].id }] : []),
    ...(none.total ? [{ id: 'noface', count: none.total, proposal: '', known: false, thumb: none.faces[0].id }] : []), ...found.groups,
    ...(state.ignored ? [{ id: 'ignored', count: state.ignored, proposal: '', known: false }] : [])];
  if (rest && !list.some((g) => g.id === rest.id)) list.splice(state.ignored ? list.length - 1 : list.length, 0, { id: rest.id, count: rest.count, proposal: rest.proposal, known: rest.known });
  let at = was ? list.findIndex((g) => g.id === was.id) : 0;
  if (at < 0 && was.known) at = list.findIndex((g) => g.known && g.proposal === was.proposal);
  if (quiet) {                        // only the list is new; what is open in the middle is not touched
    if (at >= 0 && was) Faces.open = { ...was, id: list[at].id, at };
  } else {
    if (at < 0) at = Math.min(was.at, list.length - 1);
    Faces.open = list.length ? { ...list[at], at } : null;
  }
  Faces.entries = list;
  $('face-list').replaceChildren(...list.map((group, n) => el('button', {
    class: 'face-entry' + (n === at ? ' is-open' : ''), type: 'button', role: 'option', 'aria-selected': String(n === at),
    onclick: () => openEntry(n),
  }, group.id === 'ignored' ? el('span', { class: 'face-entry__none', 'aria-hidden': 'true' }, '⊘') : el('img', { src: `api/faces/${group.thumb || group.id}/thumb`, alt: '', loading: 'lazy' }),
  el('span', { class: 'face-entry__name' + (group.known || typeof group.id === 'string' ? '' : ' is-guess') }, group.id === 'ignored' ? t('Ignoriert') : group.id === 'noface' ? t('Laut KI kein Gesicht') : group.id === 'twins' ? t('In Kopien schon benannt') : group.id === 'doubles' ? t('Doppelt im Bild') : group.known ? group.proposal : group.rest ? t('Einzelne Gesichter') : group.proposal ? t('vielleicht {0}', [group.proposal]) : t('Unbekannt')),
  el('span', { class: 'face-entry__count' }, number(group.count)))));
  const open = $('face-list').querySelector('.is-open');
  if (open && !quiet) open.scrollIntoView({ block: 'nearest' });
  if (!quiet) await showBundle();
  if (((state.enabled && state.search.running) || asking) && !$('view-faces').hidden) Faces.timer = setTimeout(() => renderFaceGroups(false, true), asking ? 6000 : 15000);
}
$('faces-more').addEventListener('click', () => renderFaceGroups(true));

// Another entry of the list: one quick question for its faces, nothing else is fetched anew.
function openEntry(n) {
  const group = Faces.entries[n];
  if (!group) { renderFaceGroups(); return; }
  Faces.open = { ...group, at: n };
  Faces.faces = FACES;
  [...$('face-list').children].forEach((entry, at) => {
    entry.classList.toggle('is-open', at === n);
    entry.setAttribute('aria-selected', String(at === n));
  });
  showBundle(true);
}

function nextEntry() {              // the bundle is done with: mark it and go on with the one below
  const at = Faces.open ? Faces.open.at : -1;
  const entry = $('face-list').children[at];
  if (entry) entry.classList.add('is-done');
  for (let n = at + 1; n < Faces.entries.length; n++) {
    if (typeof Faces.entries[n].id === 'number' && !$('face-list').children[n].classList.contains('is-done')) { openEntry(n); return; }
  }
  Faces.open = null;
  $('face-groups').replaceChildren(el('p', { class: 'field-hint' }, t('Diese Liste ist abgearbeitet. Sobald alles geschrieben ist, erscheinen die nächsten Gruppen.')));
}

async function showBundle(lazy = false) {
  $('face-ref').hidden = true;
  if (!Faces.open) { $('face-groups').replaceChildren(); return; }
  if (Faces.open.id === 'doubles') {
    if (Faces.doubles.length) $('face-groups').replaceChildren(doublesGroup(Faces.doubles)); else nextEntry();
    return;
  }
  if (Faces.open.id === 'twins') {
    const left = Faces.twins.filter((face) => !Faces.busy.has(face.id));
    if (left.length) $('face-groups').replaceChildren(twinGroup(left)); else nextEntry();
    return;
  }
  if (Faces.open.id === 'noface') {
    let none;
    try { none = await api(`faces/no-face?limit=${Faces.faces}`); } catch { $('face-groups').replaceChildren(); return; }
    $('face-groups').replaceChildren(noFaceGroup(none), ...(none.total > none.faces.length ? [el('p', {}, el('button', { class: 'fd-button', type: 'button',
      onclick: () => { Faces.faces += FACES; showBundle(); } }, t('Weitere zeigen (noch {0})', [number(none.total - none.faces.length)])))] : []));
    return;
  }
  if (Faces.open.id === 'ignored') {
    let away;
    try { away = await api(`faces/ignored?limit=${Faces.faces}&areas=${encodeURIComponent(areaQuery())}`); } catch { $('face-groups').replaceChildren(); return; }
    $('face-groups').replaceChildren(ignoredGroup(away), ...(away.total > away.faces.length ? [el('p', {}, el('button', { class: 'fd-button', type: 'button',
      onclick: () => { Faces.faces += FACES; showBundle(); } }, t('Weitere zeigen (noch {0})', [number(away.total - away.faces.length)])))] : []));
    return;
  }
  let group;
  const wanted = Faces.open.id;
  try { group = await api(`faces/groups/${wanted}?limit=${Math.min(5000, Faces.faces + Faces.busy.size)}${lazy ? LAZY : ''}&areas=${encodeURIComponent(areaQuery())}`, { quiet: true }); }
  catch { if (lazy) renderFaceGroups(); else $('face-groups').replaceChildren(); return; }       // the lists have moved on: fetch them anew
  if (!Faces.open || Faces.open.id !== wanted) return;         // somebody clicked on while this was under way
  const free = group.faces.filter((face) => !Faces.busy.has(face.id));
  if (!free.length) { nextEntry(); return; }
  const gone = group.faces.length - free.length;
  const left = Faces.open.left || new Set();           // taken out by hand before: they stay taken out
  const shown = free.slice(0, Faces.faces + left.size);
  group = { ...group, faces: shown, count: group.count - gone,
    proposal: shown.every((face) => left.has(face.id)) ? '' : Faces.open.typed || group.proposal };
  const rest = group.count - group.faces.length;
  $('face-groups').replaceChildren(faceGroup(group), ...(rest > 0 ? [el('p', {}, el('button', { class: 'fd-button', type: 'button',
    onclick: () => { Faces.faces += FACES; showBundle(true); } }, t('Weitere zeigen (noch {0})', [number(rest)])))] : []));
}

function showRef(face) {            // the whole picture beside the faces, with this face marked
  const [x, y, w, h] = face.box;
  $('face-ref').hidden = false;
  if ($('face-ref-img').dataset.key !== face.key) {
    $('face-ref-img').dataset.key = face.key;
    $('face-ref-img').src = `api/photos/${face.key}/preview?o=${face.o || 1}`;
  }
  $('face-ref-box').style.cssText = `left:${(x - w / 2) * 100}%;top:${(y - h / 2) * 100}%;width:${w * 100}%;height:${h * 100}%`;
  $('face-ref-open').onclick = () => openViewer(face.key, null);
  api('photos/' + face.key, { quiet: true }).then((photo) => {
    if ($('face-ref-img').dataset.key === face.key) $('face-ref-name').textContent = t('{0} – Klick öffnet das Bild.', [photo.path || photo.name || '']);
  }).catch(() => {});
  // a face from a second copy of a photo: show the copy that already carries the name beside it
  $('face-ref-like').hidden = !face.like;
  $('face-ref').classList.toggle('has-like', Boolean(face.like));
  if (!face.like || $('face-ref-like-img').dataset.key === face.like) return;
  $('face-ref-like-img').dataset.key = face.like;
  $('face-ref-like-img').src = `api/photos/${face.like}/preview?o=0`;
  $('face-ref-like-open').onclick = () => openViewer(face.like, null);
  $('face-ref-like-name').textContent = '';
  api('photos/' + face.like, { quiet: true }).then((photo) => {
    if ($('face-ref-like-img').dataset.key === face.like) $('face-ref-like-name').textContent = `${photo.path || ''} – ${(photo.people || []).join(', ')}`;
  }).catch(() => {});
}

function sizeFaces(px) { $('view-faces').style.setProperty('--face', px + 'px'); }

// The two side columns of this page are as wide as you pull them; a double click gives the usual width back.
function faceColumn(name, px) {
  const width = px ? Math.round(Math.max(150, Math.min(px, window.innerWidth * 0.6))) : null;
  $('view-faces').style.setProperty('--' + name, width ? width + 'px' : '');
  if (!width) $('view-faces').style.removeProperty('--' + name);
  return width;
}
for (const [grip, column, name, side] of [['face-list-grip', 'face-list-col', 'faceList', 1], ['face-ref-grip', 'face-ref', 'faceRef', -1]]) {
  const el_ = $(grip);
  const set = (px) => savePrefs({ [name]: faceColumn(name, px) });
  el_.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    el_.setPointerCapture(event.pointerId);
    el_.classList.add('is-dragging');
    const box = $(column).getBoundingClientRect();
    const move = (e) => set(side > 0 ? e.clientX - box.left : box.right - e.clientX);
    const stop = () => {
      el_.classList.remove('is-dragging');
      for (const kind of ['pointermove', 'pointerup', 'pointercancel']) el_.removeEventListener(kind, kind === 'pointermove' ? move : stop);
    };
    el_.addEventListener('pointermove', move);
    el_.addEventListener('pointerup', stop);
    el_.addEventListener('pointercancel', stop);
  });
  el_.addEventListener('dblclick', () => set(0));
  el_.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    set($(column).getBoundingClientRect().width + (event.key === 'ArrowRight' ? 20 : -20) * side);
  });
}
$('face-size').addEventListener('input', (event) => { sizeFaces(event.target.value); savePrefs({ faces: Number(event.target.value) }); });

// A face in a bundle: what a click and the three buttons do, and the one thing they cannot – deal with this face alone.
// `mark` and `on` as in pickAll(); `alone` is the entry for this single face ([text, action]) or null.
function faceMenu(event, face, mark = 'is-off', on = false, alone = null, after = null) {
  event.preventDefault();
  const pick = event.currentTarget;
  const picks = [...pick.closest('.face-group').querySelectorAll('.face-pick')];
  const set = (node, chosen) => { node.classList.toggle(mark, chosen === on); node.setAttribute('aria-pressed', String(chosen)); };
  const is = (node) => node.classList.contains(mark) === on;
  const done = () => { if (after) after(); };
  showMenu(event.clientX, event.clientY, [
    [t('Bild öffnen'), () => openViewer(face.key, null)],
    '-',
    [is(pick) ? t('Abwählen') : t('Auswählen'), () => { set(pick, !is(pick)); done(); }],
    [t('Nur dieses auswählen'), () => { for (const node of picks) set(node, node === pick); done(); }, picks.length > 1],
    [t('Alle auswählen'), () => { for (const node of picks) set(node, true); done(); }, picks.length > 1],
    [t('Keine auswählen'), () => { for (const node of picks) set(node, false); done(); }, picks.length > 1],
    [t('Auswahl umkehren'), () => { for (const node of picks) set(node, !is(node)); done(); }, picks.length > 1],
    alone && State.me.write.length ? '-' : null,
    alone && State.me.write.length ? alone : null,
  ]);
}

async function ignoreFace(face, ignored = true) {
  try {
    await api('faces/ignore', { method: 'POST', body: JSON.stringify({ faces: [face.id], ignored }) });
    await renderFaceGroups();
    message($('faces-msg'), ignored ? t('1 Gesicht ignoriert.') : t('1 Gesicht zurückgeholt – es steht wieder in den Gruppen.'));
  } catch (err) { message($('faces-msg'), explain(err.message), true); }
}

function facePick(face, alone = [t('Dieses Gesicht ignorieren'), () => ignoreFace(face)]) {
  return el('button', { class: 'face-pick', type: 'button', dataset: { id: face.id, name: face.name || '' },
    'aria-pressed': 'true', title: t('Klick: gehört nicht dazu · Umschalt-Klick: Bild öffnen · Rechtsklick: Menü'),
    oncontextmenu: (event) => faceMenu(event, face, 'is-off', false, alone),
    onmouseenter: () => showRef(face), onfocus: () => showRef(face),
    onclick: (event) => {
      showRef(face);
      if (event.shiftKey) { openViewer(face.key, null); return; }
      const off = event.currentTarget.classList.toggle('is-off');
      event.currentTarget.setAttribute('aria-pressed', String(!off));
    } }, el('img', { src: `api/faces/${face.id}/thumb`, alt: '', loading: 'lazy' }));
}

// Three small buttons for a lot of faces at once: all, none, the other way round.
// `mark` is the class that tells the two states apart; `on` says whether carrying it means "chosen".
function pickAll(picks, mark = 'is-off', on = false, after = null) {
  const set = (want) => {
    for (const pick of picks) {
      const chosen = want === null ? pick.classList.contains(mark) !== on : want;      // null: turn each one round
      pick.classList.toggle(mark, chosen === on);
      pick.setAttribute('aria-pressed', String(chosen));
    }
    if (after) after();
  };
  const button = (text, title, want) => el('button', { class: 'fd-button fd-button--transparent fd-button--compact', type: 'button', title,
    onclick: () => set(want) }, text);
  return el('span', { class: 'face-all', role: 'group', 'aria-label': t('Auswahl') },
    button(t('Alle'), t('Alle Gesichter auswählen'), true), button(t('Keine'), t('Kein Gesicht auswählen'), false),
    button(t('Umkehren'), t('Ausgewählte abwählen, abgewählte auswählen'), null));
}

$('faces-ai').addEventListener('click', async (event) => {
  const waiting = Number(event.currentTarget.dataset.waiting);
  if (!window.confirm(t('{0} unbenannte Gesichter aus den Gruppen der Unbekannten von der KI prüfen lassen?\n\n', [number(waiting)])
    + t('Gefragt wird nur: Gesicht oder nicht – nie, wer es ist. Geschickt werden die kleinen Ausschnitte, je 20 in einer Anfrage ({0} Anfragen). ', [number(Math.ceil(waiting / 20))])
    + t('Das kostet Geld beim eingestellten Anbieter; das Monatslimit gilt.\n\nIgnoriert wird dabei nichts: Was die KI für kein Gesicht hält, steht danach in einer eigenen Gruppe zum Ansehen.'))) return;
  try {
    const done = await api('faces/no-face/check', { method: 'POST', body: JSON.stringify({ limit: 50000 }) });
    message($('faces-msg'), t('Läuft: {0} Gesichter in {1} Anfragen. Abbrechen geht unter Einstellungen → Aufträge.', [number(done.started), number(done.questions)]));
  } catch (err) { message($('faces-msg'), explain(err.message), true); }
  setTimeout(() => renderFaceGroups(false, true), 1500);
});

// What the AI took for no face: a proposal. A click says "this is a face after all" – it gets a green frame
// and stays; everything without one is waved away by the one button.
function noFaceGroup(none) {
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const button = el('button', { class: 'fd-button fd-button--emphasized', type: 'button' });
  const picks = none.faces.map((face) => el('button', { class: 'face-pick', type: 'button', dataset: { id: face.id },
    'aria-pressed': 'false', title: t('Klick: ist doch ein Gesicht · Umschalt-Klick: Bild öffnen · Rechtsklick: Menü'),
    oncontextmenu: (event) => faceMenu(event, face, 'is-kept', true, null, label),
    onmouseenter: () => showRef(face), onfocus: () => showRef(face),
    onclick: (event) => {
      showRef(face);
      if (event.shiftKey) { openViewer(face.key, null); return; }
      const kept = event.currentTarget.classList.toggle('is-kept');
      event.currentTarget.setAttribute('aria-pressed', String(kept));
      label();
    } }, el('img', { src: `api/faces/${face.id}/thumb`, alt: '', loading: 'lazy' })));
  const ids = (kept) => picks.filter((pick) => pick.classList.contains('is-kept') === kept).map((pick) => Number(pick.dataset.id));
  const label = () => {
    const stay = ids(true).length; const go = ids(false).length;
    button.textContent = stay ? t('Übernehmen: {0} ignorieren, {1} {2}', [number(go), number(stay), stay === 1 ? t('ist ein Gesicht') : t('sind Gesichter')]) : t('Alle {0} ignorieren', [number(go)]);
  };
  label();
  const send = async (path, list) => {
    let count = 0;
    for (let i = 0; i < list.length; i += 200) count += (await api(path, { method: 'POST', body: JSON.stringify({ faces: list.slice(i, i + 200) }) })).done;
    return count;
  };
  button.addEventListener('click', async () => {
    button.disabled = true;
    let stay = 0; let go = 0;
    try { stay = await send('faces/no-face/keep', ids(true)); go = await send('faces/ignore', ids(false)); }
    catch (err) { message(msg, explain(err.message), true); button.disabled = false; return; }
    await renderFaceGroups();
    message($('faces-msg'), t('{0} ignoriert, {1} als Gesicht behalten.', [number(go), number(stay)]));
  });
  return el('div', { class: 'face-group' },
    el('p', { class: 'field-hint' }, t('Die KI hält {0} für kein Gesicht. Das ist nur ein Vorschlag. ', [none.total === 1 ? t('diesen Ausschnitt') : t('diese {0} Ausschnitte', [number(none.total)])])
      + t('Klick an, was doch ein Gesicht ist – es bekommt einen grünen Rahmen und bleibt als Gesicht stehen. Alles ohne Rahmen wird mit dem Knopf ignoriert.')),
    State.me.write.length ? el('div', { class: 'face-group__form' }, button, pickAll(picks, 'is-kept', true, label), msg) : null,
    el('div', { class: 'face-group__faces' }, ...picks));
}

// The faces waved away, the latest first: pick the ones that were a slip and bring them back.
function ignoredGroup(away) {
  const picks = away.faces.map((face) => {
    const pick = facePick(face, [t('Dieses Gesicht zurückholen'), () => ignoreFace(face, false)]);
    pick.classList.add('is-off'); pick.setAttribute('aria-pressed', 'false');
    pick.title = t('Klick: zurückholen · Umschalt-Klick: Bild öffnen · Rechtsklick: Menü');
    return pick;
  });
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const button = el('button', { class: 'fd-button fd-button--emphasized', type: 'button' }, t('Ausgewählte zurückholen'));
  button.addEventListener('click', async () => {
    const ids = picks.filter((pick) => !pick.classList.contains('is-off')).map((pick) => Number(pick.dataset.id));
    if (!ids.length) { message(msg, t('Kein Gesicht ausgewählt – klick die an, die zurück sollen.'), true); return; }
    button.disabled = true;
    let done = 0;
    try {
      for (let i = 0; i < ids.length; i += 200) done += (await api('faces/ignore', { method: 'POST', body: JSON.stringify({ faces: ids.slice(i, i + 200), ignored: false }) })).done;
    } catch (err) { message(msg, explain(err.message), true); button.disabled = false; return; }
    await renderFaceGroups();
    message($('faces-msg'), t('{0} zurückgeholt – {1} wieder in den Gruppen.', [done === 1 ? t('1 Gesicht') : t('{0} Gesichter', [number(done)]), done === 1 ? t('es steht') : t('sie stehen')]));
  });
  return el('div', { class: 'face-group' },
    el('p', { class: 'field-hint' }, t('{0} Gesichter sind ignoriert, jedes steht hier einmal, die zuletzt ignorierten zuerst. ', [number(away.total)])
      + t('Klick die an, die ein Versehen waren (sie werden hell), dann „Ausgewählte zurückholen“. Rechts siehst du das ganze Bild.')),
    State.me.write.length ? el('div', { class: 'face-group__form' }, button, pickAll(picks), msg) : null,
    el('div', { class: 'face-group__faces' }, ...picks));
}

// The same name twice in one picture: one of the two faces is somebody else. Pick the wrong ones – their
// name is taken off, and they stand among the unnamed faces again.
function doublesGroup(faces) {
  const names = [...new Set(faces.map((face) => face.name))];
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const button = el('button', { class: 'fd-button fd-button--emphasized', type: 'button' }, t('Bei den ausgewählten den Namen entfernen'));
  const unname = async (ids) => {
    button.disabled = true;
    const batch = randomHex();
    let good = 0; const bad = [];
    for (let i = 0; i < ids.length; i += 10) {
      message(msg, t('{0} von {1} …', [number(i), number(ids.length)]));
      let results;
      try { results = await api('faces/unname', { method: 'POST', body: JSON.stringify({ faces: ids.slice(i, i + 10), batch }) }); }
      catch (err) { results = ids.slice(i, i + 10).map(() => ({ ok: false, error: err.message })); }
      for (const result of results) if (result.ok) good += 1; else bad.push(result.error);
    }
    State.stale = true;
    loadPeople();
    await renderFaceGroups();
    message($('faces-msg'), t('{0} den Namen entfernt – {1} wieder bei den unbenannten.', [good === 1 ? t('Bei 1 Gesicht') : t('Bei {0} Gesichtern', [number(good)]), good === 1 ? t('es steht') : t('sie stehen')])
      + (bad.length ? t(' {0} nicht: {1}', [number(bad.length), [...new Set(bad.map(explain))].join(' ')]) : ''), bad.length > 0);
  };
  const picks = faces.map((face) => {
    const pick = facePick(face, [t('Namen von diesem Gesicht entfernen'), () => unname([face.id])]);
    pick.classList.add('is-off'); pick.setAttribute('aria-pressed', 'false');
    pick.title = t('Klick: das ist jemand anderes · Umschalt-Klick: Bild öffnen · Rechtsklick: Menü');
    return pick;
  });
  button.addEventListener('click', () => {
    const ids = picks.filter((pick) => !pick.classList.contains('is-off')).map((pick) => Number(pick.dataset.id));
    if (!ids.length) { message(msg, t('Kein Gesicht ausgewählt – klick die an, die jemand anderes sind.'), true); return; }
    unname(ids);
  });
  const pictures = new Set(faces.map((face) => face.key)).size;
  return el('div', { class: 'face-group' },
    el('p', { class: 'field-hint' }, t('In {0} steht derselbe Name an mehr als einem Gesicht. ', [pictures === 1 ? t('einem Bild') : t('{0} Bildern', [number(pictures)])])
      + t('Die Gesichter eines Bildes stehen nebeneinander; rechts siehst du das ganze Bild mit dem markierten Gesicht. ')
      + t('Klick die an, die jemand anderes sind (sie werden hell), dann der Knopf: Ihr Name wird aus dem Bild genommen, und sie stehen wieder bei den unbenannten Gesichtern. ')
      + t('Jede Änderung lässt sich unter Einstellungen → Änderungen zurücknehmen.')),
    State.me.write.length ? el('div', { class: 'face-group__form' }, button, pickAll(picks), msg) : null,
    ...names.map((name) => el('div', { class: 'face-twins' }, el('h4', {}, `${name} (${number(faces.filter((face) => face.name === name).length)})`),
      el('div', { class: 'face-group__faces' }, ...picks.filter((pick) => pick.dataset.name === name)))));
}

// Faces in a second copy of a photo whose first copy already names them: the names only wait for a yes.
function twinGroup(faces) {
  const names = [...new Set(faces.map((face) => face.name))];
  const picks = faces.map((face) => facePick(face));
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const button = el('button', { class: 'fd-button fd-button--emphasized', type: 'button' }, t('Namen in die Bilder schreiben'));
  button.addEventListener('click', () => {
    const chosen = picks.filter((pick) => !pick.classList.contains('is-off'));
    if (!chosen.length) { message(msg, t('Kein Gesicht ausgewählt.'), true); return; }
    for (const name of names) {
      const ids = chosen.filter((pick) => pick.dataset.name === name).map((pick) => Number(pick.dataset.id));
      if (ids.length) queueNames(name, ids);
    }
    showBundle(true);
  });
  return el('div', { class: 'face-group' },
    el('p', { class: 'field-hint' }, t('Die {0} Gesichter hier gehören nicht zusammen. Jedes steht in einem Bild, das es ein zweites Mal gibt – und in der anderen Kopie hat es schon einen Namen. ', [number(faces.length)])
      + t('Unter jedem Namen stehen die Gesichter, die ihn bekämen; rechts siehst du beide Kopien. Ein Klick nimmt ein Gesicht aus; geschrieben wird erst mit dem Knopf.')),
    State.me.write.length ? el('div', { class: 'face-group__form' }, button, pickAll(picks), msg) : null,
    ...names.map((name) => el('div', { class: 'face-twins' }, el('h4', {}, `${name} (${number(faces.filter((face) => face.name === name).length)})`),
      el('div', { class: 'face-group__faces' }, ...picks.filter((pick) => pick.dataset.name === name)))));
}

// Names are written one bundle after the other in the background, so that the page stays free for the next one.
const Writing = { queue: [], running: false, good: 0, bad: [] };

function queueNames(name, ids) {
  for (const id of ids) Faces.busy.add(id);
  Writing.queue.push({ name, ids, batch: randomHex() });
  if (!Writing.running) writeNames();
}

async function writeNames() {
  Writing.running = true;
  while (Writing.queue.length) {
    const task = Writing.queue[0];
    for (let i = 0; i < task.ids.length; i += 10) {
      const waiting = Writing.queue.length - 1;
      const open = Writing.queue.reduce((sum, other) => sum + other.ids.length, 0) - i;
      message($('faces-msg'), t('Schreibt {0}: {1} von {2}', [task.name, number(i), number(task.ids.length)])
        + (waiting ? t(' · Warteschlange: noch {0} Gesichter in {1} Gruppen', [number(open), waiting + 1]) : t(' · noch {0} Gesichter', [number(open)])));
      const part = task.ids.slice(i, i + 10);
      let results;
      try { results = await api('faces/name', { method: 'POST', body: JSON.stringify({ faces: part, name: task.name, batch: task.batch }) }); }
      catch (err) { results = part.map((face) => ({ face, ok: false, error: err.message })); }
      results.forEach((result, at) => {
        if (result.ok) { Writing.good += 1; return; }
        Writing.bad.push(result.error);
        Faces.busy.delete(part[at]);          // not written: it shows up again
      });
    }
    Writing.queue.shift();
  }
  Writing.running = false;
  const { good, bad } = Writing;
  Writing.good = 0; Writing.bad = [];
  State.stale = true;
  loadPeople();
  message($('faces-msg'), tn(good, '1 Gesicht beschriftet.', '{0} Gesichter beschriftet.')
    + (bad.length ? t(' {0} nicht: {1}', [number(bad.length), [...new Set(bad.map(explain))].join(' ')]) : ''), bad.length > 0);
  if (!$('view-faces').hidden) renderFaceGroups(false, Boolean(Faces.open));     // the list anew – quietly while a bundle is open: it stays as it is
}
window.addEventListener('beforeunload', (event) => { if (Writing.running) event.preventDefault(); });

function stayWith(picks) {
  const left = picks.filter((pick) => pick.classList.contains('is-off')).map((pick) => Number(pick.dataset.id));
  if (left.length && Faces.open) Faces.open = { ...Faces.open, keep: left[0] };
}

function faceGroup(group) {
  const left = (Faces.open && Faces.open.left) || new Set();
  const unsure = Boolean(group.maybe || group.rest);       // only a guess, or no guess at all: nothing is chosen for you
  const picks = group.faces.map((face) => {
    const pick = facePick(face);
    if (unsure || (left.has(face.id) && !group.faces.every((other) => left.has(other.id)))) { pick.classList.add('is-off'); pick.setAttribute('aria-pressed', 'false'); }
    if (unsure) pick.title = t('Klick: auswählen · Umschalt-Klick: Bild öffnen · Rechtsklick: Menü');
    return pick;
  });
  const input = el('input', { class: 'fd-input', list: 'people-names', autocomplete: 'off', maxlength: 100,
    value: group.proposal, placeholder: t('Wer ist das?'), 'aria-label': t('Name für diese Gesichter') });
  const msg = el('span', { class: 'form-msg', role: 'status' });
  const button = el('button', { class: 'fd-button fd-button--emphasized', type: 'submit' }, t('In die Bilder schreiben'));
  const may = State.me.write.length > 0;
  const chosen = () => picks.filter((pick) => !pick.classList.contains('is-off')).map((pick) => Number(pick.dataset.id));
  const discard = el('button', { class: 'fd-button fd-button--transparent', type: 'button',
    title: t('Die ausgewählten Gesichter nicht mehr vorschlagen – am Bild ändert das nichts'),
    onclick: async () => {
      const ids = chosen();
      if (!ids.length) { message(msg, t('Kein Gesicht ausgewählt.'), true); return; }
      discard.disabled = true;
      let done = 0;
      try {
        for (let i = 0; i < ids.length; i += 200) done += (await api('faces/ignore', { method: 'POST', body: JSON.stringify({ faces: ids.slice(i, i + 200) }) })).done;
      } catch (err) { message(msg, explain(err.message), true); discard.disabled = false; return; }
      stayWith(picks);
      await renderFaceGroups();
      message($('faces-msg'), t('{0} ignoriert. Zurückholen geht im einzelnen Bild oder unter Einstellungen → Personen.', [done === 1 ? t('1 Gesicht') : t('{0} Gesichter', [number(done)])]));
    } }, t('Ignorieren'));
  const form = el('form', { class: 'face-group__form' },
    el('strong', {}, group.count === 1 ? t('1 Gesicht') : t('{0} Gesichter', [number(group.count)])),
    group.count > group.faces.length ? el('span', { class: 'field-hint' }, t('– hier {0} davon', [number(group.faces.length)])) : null,
    group.maybe ? el('span', { class: 'field-hint' }, t('könnte {0} sein – nur eine Vermutung, die wahrscheinlichsten zuerst. Klick die an, die es wirklich sind.', [group.proposal]))
      : group.rest ? el('span', { class: 'field-hint' }, t('sehen niemandem ähnlich genug, die deutlichsten zuerst. Auswählen und benennen – oder „Alle“ und „Ignorieren“ für Fremde.'))
        : group.proposal ? el('span', { class: 'field-hint' }, t('sieht aus wie {0}', [group.proposal])) : null,
    may ? input : null, may ? button : null, may ? discard : null, may ? pickAll(picks) : null, msg);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const name = input.value.trim();
    const ids = chosen();
    if (!name) { message(msg, t('Bitte einen Namen eintragen.'), true); return; }
    if (!ids.length) { message(msg, t('Kein Gesicht ausgewählt.'), true); return; }
    queueNames(name, ids);      // written in the background; go on with what is left here, or with the next bundle
    if (Faces.open) {           // the name stays for the next lot; what was taken out stays taken out
      Faces.open.typed = name;
      Faces.open.left = unsure ? new Set()        // there nothing is chosen to begin with, so nothing was taken out
        : new Set(picks.filter((pick) => pick.classList.contains('is-off')).map((pick) => Number(pick.dataset.id)));
    }
    showBundle(true);
  });
  return el('div', { class: 'face-group' }, form, el('div', { class: 'face-group__faces' }, ...picks));
}

async function loadFaceSettings() {
  const state = await loadFaceSearch();
  if (!state) return;
  $('faces-areas').replaceChildren(...state.areas.map((area) => el('label', { class: 'check' },
    el('input', { type: 'checkbox', value: area.name, checked: area.enabled, disabled: Boolean(state.missing) }), ' ', el('span', {}, area.name))));
  $('faces-state').textContent = searchText(state);
  $('faces-ignored').hidden = !state.ignored;
  $('faces-ignored').firstElementChild.textContent = tn(state.ignored, '1 Gesicht ist ignoriert und wird nicht mehr vorgeschlagen.', '{0} Gesichter sind ignoriert und werden nicht mehr vorgeschlagen.');
}
/* People from Immich: a preview first, then the names go into the photos as a long job. */
const ImmichPlan = { token: '' };
$('immich-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('immich-plan').hidden = true;
  message($('immich-msg'), t('Fragt Immich … das kann bei vielen Personen etwas dauern.'));
  let found;
  try {
    found = await api('immich/preview', { method: 'POST', body: JSON.stringify({ url: $('immich-url').value, key: $('immich-key').value }) });
  } catch (err) { message($('immich-msg'), explain(err.message), true); return; }
  ImmichPlan.token = found.token;
  const todo = found.people.reduce((sum, row) => sum + row.todo, 0);
  message($('immich-msg'), found.people.length
    ? t('{0} mit Namen. {1} Namen.', [found.people.length === 1 ? t('1 Person') : t('{0} Personen', [number(found.people.length)]), found.photos === 1 ? t('1 Bild bekäme') : t('{0} Bilder bekämen', [number(found.photos)])]) : t('Immich kennt niemanden mit Namen.'));
  $('immich-people').replaceChildren(...found.people.map((row) => el('tr', {},
    el('td', {}, el('input', { type: 'checkbox', value: row.name, checked: row.todo > 0, disabled: !row.todo, 'aria-label': row.name + t(' übernehmen') })),
    el('td', {}, row.name), el('td', {}, number(row.immich)), el('td', {}, number(row.found)),
    el('td', {}, number(row.named)), el('td', {}, number(row.todo)))));
  $('immich-missed').textContent = found.missed.length
    ? t('Nicht gefunden wurden zum Beispiel: {0}. Solche Bilder liegen hier nicht oder haben hier einen anderen Namen.', [found.missed.slice(0, 5).join(', ')]) : '';
  $('immich-go').disabled = !todo;
  $('immich-plan').hidden = !found.people.length;
});
$('immich-go').addEventListener('click', async () => {
  const people = [...$('immich-people').querySelectorAll('input:checked')].map((box) => box.value);
  if (!people.length) { message($('immich-msg'), t('Niemand ausgewählt.'), true); return; }
  if (!window.confirm(t('Die Namen von {0} in die Bilder schreiben?\n\nJedes Bild steht danach bei den Änderungen und lässt sich einzeln zurücknehmen. Der Fortschritt steht oben rechts.', [people.length === 1 ? t('einer Person') : t('{0} Personen', [number(people.length)])]))) return;
  try {
    const start = await api('immich/import', { method: 'POST', body: JSON.stringify({ token: ImmichPlan.token, people }) });
    $('immich-plan').hidden = true;
    $('immich-key').value = '';
    message($('immich-msg'), t('Schreibt {0} Bilder …', [number(start.total)]));
    watchJob();
  } catch (err) { message($('immich-msg'), explain(err.message), true); }
});

$('faces-restore').addEventListener('click', async () => {
  if (!window.confirm(t('Alle ignorierten Gesichter wieder unter den unbenannten zeigen?'))) return;
  try {
    await api('faces/ignored', { method: 'DELETE' });
    message($('faces-set-msg'), t('Zurückgeholt.'));
    loadFaceSettings();
  } catch (err) { message($('faces-set-msg'), explain(err.message), true); }
});
$('faces-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const areas = Object.fromEntries([...$('faces-areas').querySelectorAll('input')].map((box) => [box.value, box.checked]));
    await api('faces', { method: 'PUT', body: JSON.stringify({ areas }) });
    message($('faces-set-msg'), t('Gespeichert.'));
    setTimeout(loadFaceSettings, 1500);
  } catch (err) { message($('faces-set-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- trash

async function loadTrashCount() {
  try {
    const n = (await api('trash')).entries.length;
    $('trash-count').textContent = n ? `(${number(n)})` : '';
  } catch { /* not important */ }
}

async function deleteShown() {
  const key = Viewer.key;
  if (!key || $('v-delete').disabled) return;
  $('v-delete').disabled = true;
  const next = (await neighbour(1)) || (await neighbour(-1));
  try {
    await api('photos/' + key, { method: 'DELETE' });
    State.dirty = true;
    State.selected.delete(key);
    for (const items of State.items.values()) {
      const at = items.findIndex((i) => i.key === key);
      if (at >= 0) items.splice(at, 1);
    }
    const button = document.querySelector(`.tile[data-key="${key}"]`);
    if (button) button.remove();
    loadTrashCount();
    if (Viewer.key === key) { if (next) await openViewer(next.key, next.month); else closeViewer(); }
    message($('v-msg'), t('In den Papierkorb gelegt.'));
  } catch (err) { message($('v-msg'), explain(err.message), true); }
  $('v-delete').disabled = false;
}
$('v-delete').addEventListener('click', deleteShown);

$('sel-delete').addEventListener('click', async () => {
  const n = State.selected.size;
  if (!window.confirm(n === 1 ? t('Das Bild in den Papierkorb legen?') : t('{0} Bilder in den Papierkorb legen?', [number(n)]))) return;
  await runMany('delete', {}, (text, isBad) => message($('sel-msg'), text, isBad), t('in den Papierkorb gelegt'));
  loadTrashCount();
});

const day = (seconds) => new Date(seconds * 1000).toLocaleDateString(LOCALE, { day: 'numeric', month: 'long', year: 'numeric' });

async function renderTrash() {
  if (!State.me) return;
  let bin;
  try { bin = await api('trash'); } catch { return; }
  const { entries, days } = bin;
  $('trash-count').textContent = entries.length ? `(${number(entries.length)})` : '';
  const size = entries.reduce((sum, e) => sum + e.size, 0);
  $('trash-line').textContent = (entries.length
    ? t('{0}, {1} MB. ', [tn(entries.length, '1 Bild', '{0} Bilder'), (size / 1048576).toLocaleString(LOCALE, { maximumFractionDigits: 1 })])
    : t('Der Papierkorb ist leer. '))
    + (days ? t('Gelöschtes bleibt {0} hier und verschwindet dann endgültig.', [days === 1 ? t('einen Tag') : days + t(' Tage')])
      : t('Gelöschtes bleibt hier, bis jemand den Papierkorb leert.'));
  $('trash-empty').hidden = !entries.some((e) => State.me.write.includes(e.area));
  const act = async (button, call, done) => {
    button.disabled = true;
    try { await call(); message($('trash-msg'), done); State.stale = true; }
    catch (err) { message($('trash-msg'), explain(err.message), true); }
    renderTrash();
  };
  $('trash-list').replaceChildren(...entries.map((entry) => {
    const may = State.me.write.includes(entry.area);
    const picture = entry.kind === 'image'
      ? el('img', { src: `api/trash/${entry.id}/thumb`, alt: '', loading: 'lazy', onerror: (e) => { e.target.style.visibility = 'hidden'; } })
      : el('span', { class: 'trash-nopic' });
    return el('div', { class: 'trash-item', oncontextmenu: may ? (event) => {
      event.preventDefault();
      const [back, gone] = event.currentTarget.querySelectorAll('.edit__buttons button');
      showMenu(event.clientX, event.clientY, [[t('Wiederherstellen'), () => back.click()], '-', [t('Endgültig löschen …'), () => gone.click()]]);
    } : null }, picture, el('div', {},
      el('strong', {}, entry.name),
      el('small', {}, entry.path.split('/').slice(0, -1).join('/')),
      el('small', {}, t('gelöscht am {0} von {1}', [day(entry.ts), entry.user])),
      el('small', {}, entry.until ? t('verschwindet am {0}', [day(entry.until)]) : t('bleibt bis zum Leeren')),
      may ? el('span', { class: 'edit__buttons' },
        el('button', { class: 'fd-button fd-button--compact', type: 'button',
          onclick: (e) => act(e.target, () => api(`trash/${entry.id}/restore`, { method: 'POST' }), t('{0} liegt wieder an seinem Platz.', [entry.name])) }, t('Wiederherstellen')),
        el('button', { class: 'fd-button fd-button--compact fd-button--transparent', type: 'button',
          onclick: (e) => { if (window.confirm(t('{0} endgültig löschen? Das lässt sich nicht zurücknehmen.', [entry.name]))) act(e.target, () => api(`trash/${entry.id}`, { method: 'DELETE' }), t('{0} ist endgültig gelöscht.', [entry.name])); } }, t('Endgültig löschen'))) : null));
  }));
}

$('trash-empty').addEventListener('click', async (event) => {
  if (!window.confirm(t('Den Papierkorb leeren? Die Bilder darin sind dann endgültig weg.'))) return;
  event.target.disabled = true;
  try {
    const { removed } = await api('trash/empty', { method: 'POST' });
    message($('trash-msg'), removed === 1 ? t('1 Bild endgültig gelöscht.') : t('{0} Bilder endgültig gelöscht.', [number(removed)]));
  } catch (err) { message($('trash-msg'), explain(err.message), true); }
  event.target.disabled = false;
  renderTrash();
});

$('trash-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await api('trash/settings', { method: 'PUT', body: JSON.stringify({ days: Number($('trash-days').value) }) });
    message($('trash-set-msg'), t('Gespeichert.'));
  } catch (err) { message($('trash-set-msg'), explain(err.message), true); }
});

// ----------------------------------------------------------------- journal

const shortDate = (iso) => new Date(iso).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'medium' });
const spot = (lat, lon) => (lat === null || lat === undefined ? t('kein Ort') : `${lat.toFixed(5)}, ${lon.toFixed(5)}`);

function describe(entry) {
  const { before, after } = entry;
  if (entry.action === 'move') return t('verschoben: {0} → {1}', [before.path, after.path]);
  if (entry.action === 'rotate') {
    const how = { 90: t('nach rechts gedreht'), 180: t('auf den Kopf gedreht'), 270: t('nach links gedreht') }[after.turn];
    if (after.lossless) return t('{0}, verlustfrei', [how]);
    const why = { raster: t('Bildgröße passt nicht ins Raster'), regions: t('die markierten Gesichter ließen sich in dieser Datei nicht mitdrehen'),
      trailer: t('an die Datei sind weitere Bilder angehängt') }[after.why] || t('nur die Markierung war zu ändern');
    return t('{0}, per Markierung ({1})', [how, why]);
  }
  if (entry.action === 'faces') return t('Personen: {0} → {1}', [before.people.join(', ') || t('keine'), after.people.join(', ') || t('keine')]);
  const parts = [];
  if ('taken' in after) parts.push(t('Datum: {0} → {1}', [before.taken_src === 'exif' ? shortDate(before.taken) : t('keins im Bild'), shortDate(after.taken)]));
  if ('lat' in after) parts.push(t('Ort: {0} → {1}', [spot(before.lat, before.lon), spot(after.lat, after.lon)]));
  if ('description' in after) parts.push(t('Beschreibung: „{0}“ → „{1}“', [before.description || '', after.description]));
  if ('keywords' in after) parts.push(t('Schlagworte: {0} → {1}', [before.keywords.join(', ') || t('keine'), after.keywords.join(', ') || t('keine')]));
  if ('rating' in after) parts.push(t('Bewertung: {0} → {1}', [before.rating || 0, after.rating]));
  if ('digitized' in after) parts.push(t('Digitalisiert am: {0} → {1}', [before.digitized || t('leer'), after.digitized || t('leer')]));
  if ('albums' in after) parts.push(t('Alben: {0} → {1}', [before.albums.join(', ') || t('keine'), after.albums.join(', ') || t('keine')]));
  return parts.join(' · ');
}

async function renderJournal() {
  const box = $('journal');
  let entries;
  try { entries = await api('journal?limit=300'); } catch { return; }
  if (!entries.length) { box.replaceChildren(el('p', { class: 'field-hint' }, t('Noch nichts geändert.'))); return; }
  const groups = [];
  for (const entry of entries) {
    const last = groups[groups.length - 1];
    if (last && last[0].batch === entry.batch) last.push(entry); else groups.push([entry]);
  }
  const body = el('tbody');
  for (const group of groups) {
    const first = group[0];
    const open = group.filter((e) => !e.undone);
    const names = group.length === 1 ? first.path : t('{0} Bilder, u. a. {1}', [group.length, first.path.split('/').pop()]);
    const same = group.every((e) => describe(e) === describe(first));
    const what = el('td', { class: open.length ? '' : 'is-undone' }, names,
      el('small', {}, same ? describe(first) : t('{0} (und ähnlich)', [describe(first)])));
    const action = el('td');
    if (!open.length) action.append(el('span', { class: 'field-hint' }, t('zurückgenommen')));
    else if (State.me.write.includes(first.area)) {
      action.append(el('button', { class: 'fd-button fd-button--compact', type: 'button', onclick: async (event) => {
        event.target.disabled = true;
        let failed = '';
        for (const entry of open) {     // newest first, the order they must be taken back in
          try { await api(`journal/${entry.id}/undo`, { method: 'POST' }); }
          catch (err) { failed = explain(err.message); break; }
        }
        message($('journal-msg'), failed || t('Zurückgenommen.'), Boolean(failed));
        renderJournal();
      } }, open.length === 1 ? t('Zurücknehmen') : t('Alle {0} zurücknehmen', [open.length])));
    }
    body.append(el('tr', {}, el('td', {}, new Date(first.ts * 1000).toLocaleString(LOCALE, { dateStyle: 'short', timeStyle: 'short' }),
      el('small', {}, first.user)), what, action));
  }
  box.replaceChildren(el('table', { class: 'journal' }, body));
}

// ----------------------------------------------------------------- status

let statusTimer = 0;
let sinceReload = 0;
async function pollStatus() {
  clearTimeout(statusTimer);
  if (!State.me || !$('view-login').hidden) return;
  let status;
  try { status = await api('status'); } catch { return; }
  const was = State.scanning;
  State.scanning = status.scan.running && status.scan.total > 0;
  $('status').textContent = State.scanning
    ? t('Liest ein: {0} von {1}', [number(status.scan.done), number(status.scan.total)])
    : t('{0} Bilder', [number(status.photos)]);
  sinceReload += 1;
  const quiet = $('viewer').hidden && $('grid-panel').scrollTop < 200 && !$('view-library').hidden;
  if ((was && !State.scanning) || (State.scanning && quiet && sinceReload >= 4)) {
    sinceReload = 0;
    State.areas = await api('areas');
    if (!State.active.size || was !== State.scanning) for (const a of State.areas) if (!store.get('areas', null)) State.active.add(a.name);
    renderChips();
    loadTree(); loadCameras(); loadPlaceNames(); reload(true);
  } else {        // the phone may have uploaded meanwhile: keep the number on the button true
    try { State.areas = await api('areas', { quiet: true }); showInbox(); } catch { /* next time */ }
  }
  statusTimer = setTimeout(pollStatus, State.scanning ? 5000 : State.inbox ? 20000 : 60000);
}

applyTheme();
start();

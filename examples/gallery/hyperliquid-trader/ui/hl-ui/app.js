/* Hyperliquid trader dashboard, plain browser JS.
   The browser joins bounded host endpoints: config, three research views, one
   wiki view, and three trader views. Each /actor/read request therefore stays
   below the chain's 512-access ceiling. Unreachable data renders explicitly;
   the page never substitutes a fabricated portfolio. */

'use strict';

function unavailableData(message) {
  return {
    mode: 'live',
    network: 'Unavailable',
    updatedBlock: 0,
    connectionError: message || 'Live actor data is unavailable',
    stats: { universe: 0, corpus: 0, theses: 0, ideas: 0, positions: 0, equity: 0, realizedPnl: 0, unrealizedPnl: 0 },
    runs: { ingest: 0, synth: 0, validate: 0, distill: 0, rebalances: 0 },
    theses: [],
    ideas: [],
    positions: [],
    account: { mode: 'unknown', network: 'unknown', spotUsdc: 0, hold: 0, available: 0, reconciledBlock: 0, corePositions: 0, xyzPositions: 0, healthy: false },
    lessons: [],
    unfillable: []
  };
}

var state = {
  data: unavailableData('Loading live actor data…'),
  filter: 'ALL',
  loading: false
};

var money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
var num = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 });

function signedPct(value) {
  return (value >= 0 ? '+' : '') + value.toFixed(2) + '%';
}

function shortAddress(value) {
  if (!value) return 'Not deployed';
  return value.slice(0, 6) + '…' + value.slice(-4);
}

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function badge(kind, label) {
  return el('span', 'badge badge-' + kind, label);
}

function sideBadge(side) {
  return badge(side === 'SHORT' ? 'short' : 'long', side);
}

function venueBadge(venue) {
  var xyz = String(venue).toLowerCase() === 'xyz';
  return badge(xyz ? 'xyz' : 'core', xyz ? 'XYZ PERP' : 'CORE PERP');
}

function metric(label, value, note) {
  var card = el('div', 'metric');
  card.appendChild(el('p', 'metric-label', label));
  card.appendChild(el('p', 'metric-value number', value));
  card.appendChild(el('p', 'metric-note', note));
  return card;
}

function banner(warn, title, body) {
  var box = el('div', 'banner ' + (warn ? 'banner-warn' : 'banner-info'));
  box.setAttribute('role', warn ? 'alert' : 'status');
  var icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('class', 'banner-icon');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('fill', 'none');
  icon.setAttribute('stroke', 'currentColor');
  icon.setAttribute('stroke-width', '2');
  icon.setAttribute('stroke-linecap', 'round');
  var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', warn
    ? 'M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'
    : 'M12 16v-4M12 8h.01M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z');
  icon.appendChild(path);
  box.appendChild(icon);
  var textWrap = el('div');
  textWrap.appendChild(el('p', 'banner-title', title));
  textWrap.appendChild(el('p', 'banner-body', body));
  box.appendChild(textWrap);
  return box;
}

function renderBanners(data) {
  var slot = document.getElementById('banner-slot');
  clear(slot);
  if (data.connectionError) {
    slot.appendChild(banner(true, 'Some live actor data is unavailable', data.connectionError));
  } else if (!data.account.healthy) {
    slot.appendChild(banner(true, 'Venue state has not reconciled',
      'The trader has not produced a valid unified-account snapshot. Keep live execution stopped until spot and perp state are synchronized.'));
  }
}

function renderAccountMetrics(data) {
  var grid = document.getElementById('account-metrics');
  clear(grid);
  var a = data.account;
  grid.appendChild(metric('Unified spot USDC', money.format(a.spotUsdc), a.mode + ' · ' + a.network));
  grid.appendChild(metric('Available', money.format(a.available), 'After maintenance'));
  grid.appendChild(metric('Margin hold', money.format(a.hold), 'Reserved by open exposure'));
  grid.appendChild(metric('Venue positions', a.corePositions + ' core · ' + a.xyzPositions + ' xyz',
    a.reconciledBlock ? 'Reconciled at block ' + num.format(a.reconciledBlock) : 'Not reconciled'));
}

function renderStatMetrics(data) {
  var grid = document.getElementById('stat-metrics');
  clear(grid);
  var s = data.stats;
  grid.appendChild(metric('Universe', String(s.universe), 'Tradable assets'));
  grid.appendChild(metric('Corpus', String(s.corpus), 'Research items'));
  grid.appendChild(metric('Theses', String(s.theses), 'Active'));
  grid.appendChild(metric('Ideas', String(s.ideas), 'Aligned'));
  grid.appendChild(metric('Positions', String(s.positions), 'Live book'));
  grid.appendChild(metric('Equity', money.format(s.equity), 'Trader NAV'));
  grid.appendChild(metric('Realized', money.format(s.realizedPnl), 'Closed P&L'));
  grid.appendChild(metric('Unrealized', money.format(s.unrealizedPnl), 'Marked P&L'));
}

function renderTheses(data) {
  var list = document.getElementById('thesis-list');
  clear(list);
  if (!data.theses.length) {
    list.appendChild(el('p', 'empty-note', 'No active thesis yet.'));
    return;
  }
  data.theses.slice(0, 2).forEach(function (thesis) {
    var row = el('div', 'thesis');
    var top = el('div', 'thesis-top');
    top.appendChild(el('p', 'thesis-statement', thesis.statement));
    top.appendChild(el('span', 'thesis-conviction number', thesis.conviction + '%'));
    row.appendChild(top);
    row.appendChild(el('p', 'thesis-context', thesis.context));
    var tags = el('div', 'thesis-tags');
    (thesis.longs || []).forEach(function (asset) { tags.appendChild(badge('long', 'LONG ' + asset)); });
    (thesis.shorts || []).forEach(function (asset) { tags.appendChild(badge('short', 'SHORT ' + asset)); });
    row.appendChild(tags);
    list.appendChild(row);
  });
}

function renderIdeas(data) {
  var list = document.getElementById('ideas-list');
  clear(list);
  var ideas = data.ideas.filter(function (idea) {
    return state.filter === 'ALL' || idea.side === state.filter;
  });
  if (!ideas.length) {
    list.appendChild(el('p', 'empty-note', 'No current ideas cleared the research gates.'));
    return;
  }
  ideas.forEach(function (idea) {
    var row = el('div', 'idea-row');
    row.appendChild(el('p', 'idea-asset', idea.asset));
    row.appendChild(sideBadge(idea.side));
    var body = el('div');
    body.appendChild(el('p', 'idea-rationale', idea.rationale));
    body.appendChild(el('p', 'idea-theme', idea.theme || 'cross-asset'));
    row.appendChild(body);
    var conf = el('div');
    var track = el('div', 'confidence-track');
    var fill = el('div', 'confidence-fill');
    fill.style.width = Math.max(0, Math.min(100, idea.confidence)) + '%';
    track.appendChild(fill);
    conf.appendChild(track);
    conf.appendChild(el('p', 'confidence-label number', idea.confidence + '% confidence'));
    row.appendChild(conf);
    list.appendChild(row);
  });
}

function positionCell(label, value) {
  var cell = el('div', 'position-cell');
  cell.appendChild(el('p', 'cell-label', label));
  cell.appendChild(el('p', 'number', value));
  return cell;
}

function renderPositions(data) {
  var list = document.getElementById('positions-list');
  clear(list);
  if (!data.positions.length) {
    list.appendChild(el('p', 'empty-note', 'The trader is flat.'));
    return;
  }
  data.positions.forEach(function (position) {
    var row = el('div', 'position-row');
    row.appendChild(el('p', 'position-asset', position.asset));
    row.appendChild(sideBadge(position.side));
    row.appendChild(venueBadge(position.venue));
    row.appendChild(positionCell('Entry', num.format(position.entry)));
    row.appendChild(positionCell('Mark', num.format(position.mark)));
    row.appendChild(positionCell('Notional', money.format(position.notional)));
    var ret = el('p', 'position-return number ' + (position.returnPct >= 0 ? 'return-up' : 'return-down'),
      signedPct(position.returnPct));
    row.appendChild(ret);
    list.appendChild(row);
  });
}

function renderPipeline(data) {
  var list = document.getElementById('pipeline-list');
  clear(list);
  var rows = [
    ['Ingest', data.runs.ingest],
    ['Synthesize', data.runs.synth],
    ['Validate', data.runs.validate],
    ['Distill', data.runs.distill],
    ['Rebalance', data.runs.rebalances]
  ];
  rows.forEach(function (pair) {
    var row = el('div', 'pipeline-row');
    row.appendChild(el('span', 'pipeline-label', pair[0]));
    row.appendChild(el('span', 'pipeline-value number', String(pair[1])));
    list.appendChild(row);
  });
}

function renderUnfillable(data) {
  var slot = document.getElementById('unfillable-slot');
  clear(slot);
  if (data.unfillable && data.unfillable.length > 0) {
    var first = data.unfillable[0];
    var reason = first.reason || (first.attempts + ' attempts without a fill');
    slot.appendChild(banner(true, 'Unfillable assets', first.asset + ': ' + reason));
  }
}

function renderLessons(data) {
  var list = document.getElementById('lessons-list');
  clear(list);
  if (!data.lessons.length) {
    list.appendChild(el('p', 'empty-note', 'No distilled lessons yet.'));
    return;
  }
  data.lessons.slice(0, 4).forEach(function (lesson) {
    list.appendChild(el('p', 'lesson', lesson));
  });
}

function renderHeader(data) {
  var live = data.mode === 'live';
  var dot = document.getElementById('live-dot');
  dot.className = 'dot ' + (live ? 'dot-live' : 'dot-demo');
  document.getElementById('network-label').textContent = data.network;
  document.getElementById('research-addr').textContent = shortAddress(data.researchActor);
  document.getElementById('trader-addr').textContent = shortAddress(data.traderActor);
  var updated = document.getElementById('updated-at');
  updated.textContent = data.updatedBlock ? 'at block ' + num.format(data.updatedBlock) : 'unavailable';
}

function render() {
  var data = state.data;
  renderHeader(data);
  renderBanners(data);
  renderAccountMetrics(data);
  renderStatMetrics(data);
  renderTheses(data);
  renderIdeas(data);
  renderPositions(data);
  renderPipeline(data);
  renderUnfillable(data);
  renderLessons(data);
}

function setLoading(loading) {
  state.loading = loading;
  var btn = document.getElementById('refresh-btn');
  var icon = document.getElementById('refresh-icon');
  btn.disabled = loading;
  icon.className = loading ? 'refresh-icon spinning' : 'refresh-icon';
}

function fetchPart(path) {
  return fetch(path, { cache: 'no-store' }).then(function (response) {
    if (!response.ok) throw new Error(path + ' returned ' + response.status);
    return response.json();
  });
}

function mergeDashboard(base, research, lessons, trader) {
  var data = unavailableData('');
  data.mode = base.mode || 'live';
  data.network = base.network || 'Cowboy';
  data.updatedBlock = base.updatedBlock || 0;
  data.researchActor = base.researchActor;
  data.traderActor = base.traderActor;
  data.stats = Object.assign({}, data.stats, research.stats || {}, trader.stats || {});
  data.runs = Object.assign({}, data.runs, research.runs || {}, trader.runs || {});
  data.theses = research.theses || [];
  data.ideas = research.ideas || [];
  data.lessons = lessons.lessons || [];
  data.positions = trader.positions || [];
  data.account = Object.assign({}, data.account, trader.account || {});
  data.unfillable = trader.unfillable || [];
  data.connectionError = [research.connectionError, trader.connectionError]
    .filter(Boolean).join('; ');
  return data;
}

function refresh() {
  setLoading(true);
  Promise.all([
    fetchPart('api/dashboard'),
    fetchPart('api/research'),
    fetchPart('api/research/lessons'),
    fetchPart('api/trader')
  ])
    .then(function (parts) {
      state.data = mergeDashboard(parts[0], parts[1], parts[2], parts[3]);
    })
    .catch(function (error) {
      state.data = unavailableData(error && error.message ? error.message : 'Unable to refresh');
    })
    .then(function () {
      setLoading(false);
      render();
    });
}

document.getElementById('refresh-btn').addEventListener('click', refresh);

document.getElementById('idea-filters').addEventListener('click', function (event) {
  var target = event.target.closest('button[data-filter]');
  if (!target) return;
  state.filter = target.getAttribute('data-filter');
  var buttons = document.querySelectorAll('#idea-filters .filter-btn');
  buttons.forEach(function (button) {
    button.className = 'filter-btn' + (button === target ? ' filter-active' : '');
  });
  renderIdeas(state.data);
});

render();
refresh();

/* Tiny table sort/filter helper. No dependencies, no build step.
   Opt in with data-sortable on a <table>; columns declare data-type. */
(function () {
  'use strict';

  function cellValue(row, index, type) {
    var cell = row.cells[index];
    if (!cell) return type === 'text' ? '' : -Infinity;
    var raw = cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent;
    if (type === 'text') return raw.trim().toLowerCase();
    var num = parseFloat(String(raw).replace(/[^0-9.eE+-]/g, ''));
    return isNaN(num) ? -Infinity : num;
  }

  /* An expanded row adds a second <tr> straight after its own. Sorting and
     filtering both work on the player rows alone and carry the detail along,
     so re-sorting an open row does not strand its panel under someone else. */
  function mainRows(table) {
    return Array.prototype.filter.call(table.tBodies[0].rows, function (row) {
      return row.dataset.detailFor === undefined;
    });
  }

  function detailFor(row) {
    var next = row.nextElementSibling;
    return next && next.dataset.detailFor !== undefined ? next : null;
  }

  function sortTable(table, index, type, direction) {
    var body = table.tBodies[0];
    var rows = mainRows(table);
    rows.sort(function (a, b) {
      var av = cellValue(a, index, type);
      var bv = cellValue(b, index, type);
      if (av < bv) return -direction;
      if (av > bv) return direction;
      return 0;
    });
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) {
      // Read the pairing before moving anything: appending detaches the row.
      var detail = detailFor(row);
      fragment.appendChild(row);
      if (detail) fragment.appendChild(detail);
    });
    body.appendChild(fragment);
  }

  function initSorting(table) {
    var headers = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(headers, function (th, index) {
      if (th.dataset.nosort !== undefined) return;
      th.classList.add('sortable');
      th.tabIndex = 0;
      var type = th.dataset.type || 'number';

      function activate() {
        var wasDesc = th.classList.contains('sort-desc');
        // Numbers are most useful highest-first; text A-Z.
        var direction = wasDesc ? 1 : (th.classList.contains('sort-asc') ? -1 : (type === 'text' ? 1 : -1));
        Array.prototype.forEach.call(headers, function (other) {
          other.classList.remove('sort-asc', 'sort-desc');
        });
        th.classList.add(direction === 1 ? 'sort-asc' : 'sort-desc');
        sortTable(table, index, type, direction);
      }

      th.addEventListener('click', activate);
      th.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate();
        }
      });
    });
  }

  function initFilters(table) {
    var controls = document.querySelectorAll('[data-filter-for="' + table.id + '"]');
    if (!controls.length) return;
    var counter = document.querySelector('[data-count-for="' + table.id + '"]');

    function apply() {
      var rows = mainRows(table);
      var shown = 0;
      rows.forEach(function (row) {
        var visible = true;
        Array.prototype.forEach.call(controls, function (control) {
          if (!visible) return;
          var value = control.value;
          if (value === '' || value === null) return;
          var key = control.dataset.filterKey;
          var mode = control.dataset.filterMode || 'equals';
          var actual = row.dataset[key] || '';
          if (mode === 'equals' && actual !== value) visible = false;
          if (mode === 'contains' && actual.toLowerCase().indexOf(value.toLowerCase()) === -1) visible = false;
          if (mode === 'max' && parseFloat(actual) > parseFloat(value)) visible = false;
          if (mode === 'min' && parseFloat(actual) < parseFloat(value)) visible = false;
        });
        row.hidden = !visible;
        var detail = detailFor(row);
        if (detail) detail.hidden = !visible;
        if (visible) shown++;
      });
      if (counter) counter.textContent = shown + ' of ' + rows.length;
    }

    Array.prototype.forEach.call(controls, function (control) {
      control.addEventListener('input', apply);
      control.addEventListener('change', apply);
    });
    apply();
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('table[data-sortable]').forEach(function (table) {
      initSorting(table);
      if (table.id) initFilters(table);
      initCellTips(table);
      if (table.dataset.opponentsBase) initExpanders(table);
    });

    // Disable the refresh button once clicked so it can't be double-fired.
    document.querySelectorAll('form[data-refresh]').forEach(function (form) {
      form.addEventListener('submit', function () {
        var button = form.querySelector('button');
        if (button) {
          button.disabled = true;
          button.textContent = 'Refreshing…';
        }
      });
    });

    initTabs();
    initTableFill();
    initMatchweekStrip();
    initAutoUpdate();
  });

  /* Sub-tabs. Every panel is already in the page, so switching is a matter of
     which one is hidden — no reload, and the tables inside keep whatever sort
     the reader left them in. Groups are named with data-tab-for so more than one
     set of tabs can live on a page. */
  function initTabs() {
    document.querySelectorAll('[data-tab-for]').forEach(function (button) {
      button.addEventListener('click', function () {
        var group = button.dataset.tabFor;
        var wanted = button.dataset.tab;
        document.querySelectorAll('[data-tab-for="' + group + '"]').forEach(function (other) {
          var on = other === button;
          other.classList.toggle('on', on);
          other.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.querySelectorAll('[data-tab-panel="' + group + '"]').forEach(function (panel) {
          panel.hidden = panel.dataset.tab !== wanted;
        });
      });
    });
  }

  /* Scroll the matchweek you are on into the middle of its strip.

     Thirty-eight weeks do not fit across a window, so by March the one you are
     looking at is off the left-hand end of a bar that looks, at a glance, like
     it starts at week 1. Centring it is what makes the strip read as a position
     in the season rather than a list that happens to be cut off. `block: nearest`
     keeps the page itself still — without it the browser scrolls the whole
     document down to the bar. */
  function initMatchweekStrip() {
    var strip = document.querySelector('[data-mw-strip]');
    if (!strip) return;
    var current = strip.querySelector('[data-mw-current]');
    if (!current) return;
    if (current.scrollIntoView) {
      current.scrollIntoView({ block: 'nearest', inline: 'center' });
    } else {
      strip.scrollLeft = current.offsetLeft - (strip.clientWidth - current.clientWidth) / 2;
    }
  }

  /* Carry each column's explanation down the table.

     The header says what every column is, but 400 rows down the header is a
     long way off and the values are bare numbers. Each cell borrows its
     column's tooltip the first time you point at it — lent on hover rather than
     stamped into the HTML, because 650 rows of twenty identical sentences would
     roughly triple the size of the page. A cell that already explains itself
     keeps its own text underneath the column's. */
  function initCellTips(table) {
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    var tips = Array.prototype.map.call(heads, function (th) {
      var tip = th.getAttribute('title');
      if (!tip) return '';
      var label = (th.textContent || '').trim();
      return label ? label + ' — ' + tip : tip;
    });
    if (!tips.some(Boolean)) return;

    table.addEventListener('mouseover', function (event) {
      var cell = event.target.closest && event.target.closest('td');
      if (!cell || cell.dataset.tipped !== undefined) return;
      var row = cell.parentNode;
      // The expanded panel spans every column; it has no column of its own.
      if (!row || row.dataset.detailFor !== undefined) return;
      cell.dataset.tipped = '';
      var tip = tips[cell.cellIndex];
      if (!tip) return;
      var own = cell.getAttribute('title');
      cell.setAttribute('title', own ? tip + '\n\n' + own : tip);
    });
  }

  /* Give the explorer's table its own scroll box, sized to the space left
     under it.

     With the page doing the scrolling, a 20-column table put its horizontal
     scrollbar at the foot of 650 rows — reachable only by scrolling past every
     player. Scrolling inside a box fixes that, but only if the box fits on
     screen: a box taller than the viewport takes its sticky header up out of
     view with it. CSS cannot size this, because calc() has no way to ask where
     the box starts, so it is measured. */
  function initTableFill() {
    var box = document.querySelector('.table-scroll.fill');
    if (!box) return;

    function fit() {
      box.style.maxHeight = '';
      var top = box.getBoundingClientRect().top;
      // Never so short that the table is a letterbox — on a small screen it is
      // better to let the page scroll than to show four rows at a time.
      box.style.maxHeight = Math.max(320, window.innerHeight - top - 14) + 'px';
    }

    fit();
    // Resize only: measuring on scroll would fight itself, since changing the
    // height changes the page height and so the scroll position.
    window.addEventListener('resize', fit);
  }

  /* --- Expandable per-opponent history -----------------------------------

     The table ships without any of this: six opponents' worth of match history
     for seven hundred players is far more markup than a page needs, and almost
     none of it is ever opened. A row fetches its own when you open it, and the
     answer is kept for the rest of the visit. */
  var opponentCache = {};

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function initExpanders(table) {
    var base = table.dataset.opponentsBase;

    table.addEventListener('click', function (event) {
      var button = event.target.closest('[data-expand]');
      if (!button) return;
      var row = button.closest('tr');
      if (!row) return;
      event.preventDefault();
      toggle(table, base, row, button);
    });
  }

  function toggle(table, base, row, button) {
    var open = detailFor(row);
    if (open) {
      open.remove();
      button.setAttribute('aria-expanded', 'false');
      row.classList.remove('expanded');
      return;
    }

    var detail = row.ownerDocument.createElement('tr');
    detail.dataset.detailFor = row.dataset.player || '';
    detail.className = 'detail-row';
    var cell = row.ownerDocument.createElement('td');
    cell.colSpan = row.cells.length;
    cell.innerHTML = '<div class="detail is-loading">Looking up the record…</div>';
    detail.appendChild(cell);
    row.parentNode.insertBefore(detail, row.nextSibling);
    button.setAttribute('aria-expanded', 'true');
    row.classList.add('expanded');

    var id = row.dataset.player;
    if (opponentCache[id]) {
      cell.innerHTML = renderDetail(opponentCache[id]);
      return;
    }
    fetch(base + '/' + encodeURIComponent(id) + '/opponents', {
      headers: { 'Accept': 'application/json' }, cache: 'no-store'
    })
      .then(function (response) {
        if (!response.ok) throw new Error('status ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        opponentCache[id] = payload;
        cell.innerHTML = renderDetail(payload);
      })
      .catch(function () {
        cell.innerHTML = '<div class="detail is-loading">Could not load the record — '
          + 'the app may be mid-refresh. Close and reopen to try again.</div>';
      });
  }

  function fixtureLabel(fixture) {
    return '<span class="opp-gw">GW' + esc(fixture.event) + '</span>'
      + '<span class="tick fdr-' + esc(fixture.difficulty || 3) + (fixture.home ? '' : ' away')
      + '" title="' + esc(fixture.home ? 'Home to ' : 'Away to ') + esc(fixture.opp_name) + '">'
      + esc(fixture.opp) + '</span>';
  }

  function when(match, fixture) {
    return match.season + ' GW' + match.round + ' · '
      + (match.home ? 'home to ' : 'away to ') + fixture.opp_name;
  }

  /* An empty run is three different statements and they are not interchangeable:
     the club is new to the league, the opponent is, or the two have simply not
     met in the seasons that are loaded. Saying which is the whole point. */
  function nothing(fixture, player) {
    var why = 'no meetings in the loaded seasons';
    if (!player.club_known) why = 'no data — ' + player.team_name + ' is new to the league';
    else if (!fixture.known) why = 'no data — ' + fixture.opp_name + ' is new to the league';
    return '<span class="meet none">' + esc(why) + '</span>';
  }

  function teamLine(fixture, player) {
    var runs = fixture.team_form || [];
    if (!runs.length) return nothing(fixture, player);
    return runs.map(function (m) {
      var kind = m.result === 'W' ? 'win' : (m.result === 'L' ? 'loss' : 'draw');
      return '<span class="meet ' + kind + '" title="' + esc(when(m, fixture)) + '">'
        + '<b>' + esc(m.result) + '</b> ' + esc(m.scored) + '–' + esc(m.against) + '</span>';
    }).join('');
  }

  function playerLine(fixture, player) {
    var threshold = player.defcon_threshold;
    var runs = fixture.player_form || [];
    if (!runs.length) {
      return fixture.known && player.club_known
        ? '<span class="meet none">no appearances against them</span>'
        : nothing(fixture, player);
    }
    return runs.map(function (m) {
      var defcon = '<span class="pf">DC —</span>';
      if (m.defcon !== null && m.defcon !== undefined) {
        // A count on its own means nothing without the bar it had to clear.
        var hit = threshold && m.defcon >= threshold;
        defcon = '<span class="pf' + (hit ? ' on' : '') + '">DC ' + esc(m.defcon) + '</span>';
      }
      return '<span class="perf" title="' + esc(when(m, fixture)) + ' · '
        + esc(m.minutes) + ' mins · ' + esc(m.points) + ' pts">'
        + '<span class="pf-min">' + esc(m.minutes) + '\u2032</span>'
        + '<span class="pf' + (m.goals ? ' on' : '') + '">G ' + esc(m.goals) + '</span>'
        + '<span class="pf' + (m.assists ? ' on' : '') + '">A ' + esc(m.assists) + '</span>'
        + defcon + '</span>';
    }).join('');
  }

  function renderBlock(title, note, fixtures, player, line) {
    var rows = fixtures.map(function (fixture) {
      return '<div class="opp-line"><span class="opp-fix">' + fixtureLabel(fixture)
        + '</span><span class="opp-runs">' + line(fixture, player) + '</span></div>';
    }).join('');
    return '<section class="detail-block"><h4 class="detail-title">' + esc(title)
      + '<span class="detail-note">' + esc(note) + '</span></h4>'
      + '<div class="opp-list">' + rows + '</div></section>';
  }

  function renderDetail(payload) {
    var player = payload.player || {};
    var fixtures = payload.fixtures || [];
    var limit = payload.limit || 5;
    if (!fixtures.length) {
      return '<div class="detail is-loading">No upcoming fixtures are projected for '
        + esc(player.name) + ' — run a refresh, or the season may be over.</div>';
    }
    return '<div class="detail">'
      + renderBlock(
          (player.team_name || 'The club') + ' against these opponents',
          'last ' + limit + ', most recent first',
          fixtures, player, teamLine)
      + renderBlock(
          (player.name || 'This player') + '\u2019s own record',
          'goals, assists and defensive contributions, appearances only',
          fixtures, player, playerLine)
      + '</div>';
  }

  /* Keep an open page in step with jobs that finish elsewhere.

     The refresh and crowd jobs run from cron, in their own processes, so there
     is nothing to push from — the page polls a cheap /api/status instead and
     reloads itself when the data version it was served with stops matching. */
  function initAutoUpdate() {
    var body = document.body;
    var url = body.dataset.statusUrl;
    var version = body.dataset.version;
    if (!url || !version) return;

    var IDLE_MS = 60000;      // nothing happening: once a minute is plenty
    var BUSY_MS = 5000;       // a job is mid-run: watch it closely
    var timer = null;
    var stopped = false;
    var pendingReload = false;

    var button = document.querySelector('[data-refresh-button]');
    var idleLabel = button ? button.textContent.trim() : '';

    /* Reloading mid-keystroke would throw away a half-typed filter, so wait
       until the field is given up rather than yanking the page away. */
    function busyTyping() {
      var el = document.activeElement;
      if (!el) return false;
      var tag = el.tagName;
      return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
    }

    function reload() {
      if (busyTyping()) {
        pendingReload = true;
        return;
      }
      stopped = true;
      window.location.reload();
    }

    function showJob(status) {
      if (!button) return;
      if (status.running) {
        button.disabled = true;
        button.textContent = status.stage
          ? 'Refreshing: ' + status.stage + '…'
          : 'Refreshing…';
      } else if (button.disabled) {
        button.disabled = false;
        button.textContent = idleLabel || 'Refresh now';
      }
    }

    function poll() {
      if (stopped || document.hidden) return schedule(IDLE_MS);
      fetch(url, { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
        .then(function (response) {
          if (!response.ok) throw new Error('status ' + response.status);
          return response.json();
        })
        .then(function (status) {
          if (status.version && status.version !== version) return reload();
          showJob(status);
          schedule(status.running ? BUSY_MS : IDLE_MS);
        })
        .catch(function () {
          // App restarting or Pi asleep — keep trying, just not hard.
          schedule(IDLE_MS);
        });
    }

    function schedule(delay) {
      window.clearTimeout(timer);
      if (!stopped) timer = window.setTimeout(poll, delay);
    }

    // A tab that has been in the background is the most likely to be stale.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) poll();
    });
    document.addEventListener('focusout', function () {
      if (pendingReload) reload();
    });

    schedule(IDLE_MS);
  }
})();

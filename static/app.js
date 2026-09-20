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
      initCompare(table);
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
      // A cell in the season log inside an expanded panel has a cellIndex of
      // its own table, which means nothing here — column 5 there is not column
      // 5 of the explorer, and lending it xA/90's tooltip would be a lie.
      if (cell.closest('table') !== table) return;
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
  var fitFilled = function () {};

  function initTableFill() {
    var boxes = document.querySelectorAll('.table-scroll.fill');
    if (!boxes.length) return;

    fitFilled = function () {
      boxes.forEach(function (box) {
        // A hidden box measures as sitting at the top of the document, which
        // would size it to the whole window and leave it that way when shown.
        if (box.offsetParent === null) return;
        // Sized by its own contents for now — the comparison strip, which is a
        // few rows rather than a page of them.
        if (box.dataset.fillOff !== undefined) return;
        box.style.maxHeight = '';
        var top = box.getBoundingClientRect().top;
        // Room claimed by something parked below, so the two do not fight over
        // the same stretch of window.
        var reserved = parseFloat(box.dataset.fillReserve) || 0;
        // Never so short that the table is a letterbox — on a small screen it is
        // better to let the page scroll than to show four rows at a time. The
        // floor comes down when something useful is parked below: a stubby table
        // over a strip of picks is a different thing from a stubby table alone.
        var floor = parseFloat(box.dataset.fillFloor) || 320;
        box.style.maxHeight = Math.max(floor, window.innerHeight - top - 14 - reserved) + 'px';
      });
    };

    fitFilled();
    // Resize only: measuring on scroll would fight itself, since changing the
    // height changes the page height and so the scroll position.
    window.addEventListener('resize', fitFilled);
  }

  /* --- Side by side --------------------------------------------------------

     Twenty columns down six hundred rows is a good way to find a player and a
     poor way to choose between two: the pair you are weighing up are forty rows
     apart, and picking between them means scrolling back and forth holding six
     numbers in your head. Ticking a row lifts it out into a panel beside the
     table, turned on its side — a column per player, a row per stat — so the
     comparison is read across a line instead of remembered.

     Nothing here is fetched. Every value in the panel is the very cell from the
     table, colour band and tooltip and all, which is what keeps the two halves
     of the page from ever disagreeing about a number. */
  var COMPARE_STORE = 'fpl-compare';
  var ORIENT_STORE = 'fpl-compare-orient';

  function initCompare(table) {
    var panel = document.querySelector('[data-compare-panel]');
    if (!panel || !table.tHead) return;
    var layout = document.querySelector('[data-compare-layout]');
    var host = panel.querySelector('[data-compare-table]');
    var counter = panel.querySelector('[data-compare-count]');
    var strip = panel.querySelector('.compare-scroll');
    var mainBox = table.closest('.table-scroll');

    /* Every column of the table above, and what the panel may do with it.
       `skip` marks the three that are the heading of a player rather than a
       statistic — name, position, club — which the beside layout lifts out and
       the below layout keeps in place. `best` marks the ones with a better end:
       £m and Own % carry none, for the same reason they carry no colour band in
       the table, and neither does a fixture run. */
    var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);
    var allColumns = heads.map(function (th) {
      return {
        index: th.cellIndex,
        label: (th.textContent || '').trim(),
        tip: th.getAttribute('title') || '',
        best: th.dataset.best || '',
        skip: th.dataset.compareSkip !== undefined
      };
    });
    var statColumns = allColumns.filter(function (column) { return !column.skip; });

    // Order of ticking, not of the table: a player never moves under the cursor
    // because someone further up the table was added after them.
    var picked = restore();
    var orient = restoreOrient();

    function restore() {
      try {
        var raw = window.sessionStorage.getItem(COMPARE_STORE);
        var saved = raw ? JSON.parse(raw) : [];
        return Array.isArray(saved) ? saved.map(String) : [];
      } catch (error) {
        return [];
      }
    }

    function restoreOrient() {
      try {
        return window.sessionStorage.getItem(ORIENT_STORE) === 'columns' ? 'columns' : 'rows';
      } catch (error) {
        return 'rows';
      }
    }

    /* The page reloads itself whenever a refresh lands elsewhere, and losing a
       half-built comparison to that would be maddening. Session-scoped, so it
       is remembered across the reload and forgotten when the tab closes. */
    function remember() {
      try {
        window.sessionStorage.setItem(COMPARE_STORE, JSON.stringify(picked));
        window.sessionStorage.setItem(ORIENT_STORE, orient);
      } catch (error) {
        // Private mode or a full store: the panel still works, it just forgets.
      }
    }

    function rowFor(id) {
      return table.querySelector('tbody tr[data-player="' + id + '"]');
    }

    function numberIn(cell) {
      if (!cell) return NaN;
      var raw = cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent;
      return parseFloat(String(raw).replace(/[^0-9.eE+-]/g, ''));
    }

    /* Which of the ticked players leads a column — and, as often, that none of
       them does. One player is not a contest, and a stat where everyone holds
       the same number separates nobody, so marking it would be decoration
       dressed up as a finding. Genuine ties are all marked. */
    function leaders(cells, direction) {
      if (cells.length < 2 || !direction) return [];
      var values = cells.map(numberIn);
      var real = values.filter(function (value) { return !isNaN(value); });
      if (real.length < 2) return [];
      var top = direction === 'low' ? Math.min.apply(null, real) : Math.max.apply(null, real);
      var hits = [];
      values.forEach(function (value, i) { if (value === top) hits.push(i); });
      return hits.length === values.length ? [] : hits;
    }

    function crown(cell) {
      cell.classList.add('is-best');
      var own = cell.getAttribute('title');
      cell.setAttribute('title', (own ? own + ' · ' : '') + 'best of the ticked players');
    }

    /* --- Beside: a column per player, a row per stat --------------------- */

    function besideHead(row) {
      var name = row.querySelector('td.name');
      var short = name ? (name.dataset.sort || (name.textContent || '').trim()) : '?';
      var full = (name && name.getAttribute('title')) || short;
      var sub = [row.dataset.pos, row.dataset.team,
                 row.dataset.price ? '£' + row.dataset.price + 'm' : ''];
      return '<th class="cmp-head" title="' + esc(full) + '">'
        + dropButton(row, short)
        + '<span class="cmp-name">' + esc(short) + '</span>'
        + '<span class="cmp-sub">' + esc(sub.filter(Boolean).join(' · ')) + '</span></th>';
    }

    /* The cell itself, moved rather than rebuilt: same markup, same band, so a
       ticker stays a ticker and a thin sample stays grey. Only the tooltip is
       rewritten, because out here there is no column header above it to say
       which stat this is. */
    function besideCell(cell, column, best) {
      if (!cell) return '<td class="num"><span class="miss">—</span></td>';
      var tip = [column.label];
      var own = cell.getAttribute('title');
      if (own) tip.push(own);
      if (best) tip.push('best of the ticked players');
      return '<td class="' + esc(cell.className + (best ? ' is-best' : ''))
        + '" title="' + esc(tip.join(' · ')) + '">' + cell.innerHTML + '</td>';
    }

    function renderBeside(rows) {
      var head = '<thead><tr><th class="cmp-corner"></th>'
        + rows.map(besideHead).join('') + '</tr></thead>';
      var body = statColumns.map(function (column) {
        var cells = rows.map(function (row) { return row.cells[column.index]; });
        var best = leaders(cells, column.best);
        return '<tr><th class="cmp-stat" title="' + esc(column.tip) + '">'
          + esc(column.label) + '</th>'
          + cells.map(function (cell, i) {
              return besideCell(cell, column, best.indexOf(i) !== -1);
            }).join('')
          + '</tr>';
      }).join('');
      host.className = 'cmp cmp-beside';
      host.style.width = '';
      host.innerHTML = head + '<tbody>' + body + '</tbody>';
    }

    /* --- Below: a row per player, under the columns they came from -------

       The rows here are clones of the rows above, so the strip cannot disagree
       with the table about a number even in principle. What it does need is the
       table's column widths, which only exist once the browser has laid 650
       rows out — hence the measured <colgroup> and the fixed layout that makes
       it stick. syncWidths keeps the two in step afterwards. */

    function dropButton(row, short) {
      return '<button type="button" class="cmp-drop" data-drop="' + esc(row.dataset.player)
        + '" title="' + esc('Drop ' + short + ' from the comparison') + '">×</button>';
    }

    function renderBelow(rows) {
      var head = table.tHead.rows[0].cloneNode(true);
      // The sorting affordances belong to the table above; a second set of
      // arrows here would promise something these headers do not do.
      Array.prototype.forEach.call(head.cells, function (th) {
        th.classList.remove('sortable', 'sort-asc', 'sort-desc');
        th.removeAttribute('tabindex');
      });
      head.cells[0].textContent = '';

      var clones = rows.map(function (row) {
        var clone = row.cloneNode(true);
        // A pick stays in the strip while the table is filtered down past it,
        // and `hidden` rides along on the clone if it is not taken off.
        clone.hidden = false;
        clone.classList.remove('picked', 'expanded');
        var caret = clone.querySelector('[data-expand]');
        if (caret) caret.remove();
        var name = row.querySelector('td.name');
        clone.cells[0].innerHTML = dropButton(row, name ? name.dataset.sort : '');
        return clone;
      });

      allColumns.forEach(function (column) {
        var cells = rows.map(function (row) { return row.cells[column.index]; });
        leaders(cells, column.best).forEach(function (i) {
          crown(clones[i].cells[column.index]);
        });
      });

      var group = document.createElement('colgroup');
      heads.forEach(function () { group.appendChild(document.createElement('col')); });

      host.className = 'cmp cmp-below';
      host.innerHTML = '';
      host.appendChild(group);
      var thead = document.createElement('thead');
      thead.appendChild(head);
      host.appendChild(thead);
      var body = document.createElement('tbody');
      clones.forEach(function (clone) { body.appendChild(clone); });
      host.appendChild(body);
      syncWidths();
    }

    function syncWidths() {
      if (orient !== 'rows' || panel.hidden) return;
      var cols = host.querySelectorAll('col');
      if (!cols.length) return;
      var total = 0;
      heads.forEach(function (th, i) {
        var width = th.getBoundingClientRect().width;
        total += width;
        if (cols[i]) cols[i].style.width = width + 'px';
      });
      host.style.width = total + 'px';
    }

    /* Two scroll boxes showing the same columns have to move together, or the
       strip stops being under the table and starts being a second opinion. */
    function linkScroll(a, b) {
      var settling = false;
      function mirror(from, to) {
        return function () {
          if (settling || orient !== 'rows') return;
          settling = true;
          to.scrollLeft = from.scrollLeft;
          window.requestAnimationFrame(function () { settling = false; });
        };
      }
      a.addEventListener('scroll', mirror(a, b));
      b.addEventListener('scroll', mirror(b, a));
    }

    /* --- Sizing ---------------------------------------------------------

       Beside, the two panels stand side by side and each takes the window's
       full height. Below, they are stacked and share it: the strip is measured
       first, because its height depends only on how many players are in it, and
       the table is then told to stop short of it. */
    function resize() {
      strip.style.maxHeight = '';
      if (orient === 'rows' && !panel.hidden && mainBox) {
        strip.dataset.fillOff = '';
        // Measured rather than added up: everything between the foot of the
        // table's scroll box and the foot of the strip — the gap, two borders,
        // the panel's own head — is one distance, and it does not change with
        // the height the table is about to be given. Adding up the parts got it
        // wrong by the sixteen pixels that hung off the bottom of the window.
        var below = panel.getBoundingClientRect().bottom - mainBox.getBoundingClientRect().bottom;
        mainBox.dataset.fillReserve = Math.max(0, Math.round(below));
        mainBox.dataset.fillFloor = 240;
      } else {
        delete strip.dataset.fillOff;
        if (mainBox) {
          delete mainBox.dataset.fillReserve;
          delete mainBox.dataset.fillFloor;
        }
      }
      fitFilled();
    }

    function render() {
      // A stored id with no row behind it is dropped rather than left as a gap:
      // a refresh can retire a player between one visit to the page and the next.
      var rows = [];
      picked = picked.filter(function (id) {
        var row = rowFor(id);
        if (row) rows.push(row);
        return !!row;
      });
      remember();

      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var box = row.querySelector('input[data-compare]');
        if (!box) return;
        var on = picked.indexOf(row.dataset.player) !== -1;
        box.checked = on;
        row.classList.toggle('picked', on);
      });

      panel.hidden = !rows.length;
      panel.dataset.orient = orient;
      if (layout) {
        layout.classList.toggle('comparing', rows.length > 0);
        layout.classList.toggle('stacked', orient === 'rows' && rows.length > 0);
      }
      panel.querySelectorAll('button[data-orient]').forEach(function (button) {
        var on = button.dataset.orient === orient;
        button.classList.toggle('on', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      counter.textContent = rows.length + (rows.length === 1 ? ' player' : ' players');

      if (!rows.length) host.innerHTML = '';
      else if (orient === 'rows') renderBelow(rows);
      else renderBeside(rows);
      resize();
    }

    table.addEventListener('change', function (event) {
      var box = event.target.closest && event.target.closest('input[data-compare]');
      if (!box) return;
      var row = box.closest('tr');
      if (!row) return;
      var id = row.dataset.player;
      var at = picked.indexOf(id);
      if (box.checked && at === -1) picked.push(id);
      if (!box.checked && at !== -1) picked.splice(at, 1);
      render();
    });

    panel.addEventListener('click', function (event) {
      if (!event.target.closest) return;
      var drop = event.target.closest('[data-drop]');
      if (drop) {
        var at = picked.indexOf(drop.dataset.drop);
        if (at !== -1) picked.splice(at, 1);
        render();
        return;
      }
      var turn = event.target.closest('button[data-orient]');
      if (turn) {
        orient = turn.dataset.orient;
        render();
        return;
      }
      if (event.target.closest('[data-compare-clear]')) {
        picked = [];
        render();
      }
    });

    /* Sorting, filtering and a window resize all move the table's columns about,
       and the strip's own widths are copies of theirs. Watching the headers
       catches all three without any of them having to know about the strip. */
    if (window.ResizeObserver) {
      var watcher = new window.ResizeObserver(syncWidths);
      heads.forEach(function (th) { watcher.observe(th); });
    }
    if (mainBox) linkScroll(mainBox, strip);
    window.addEventListener('resize', resize);

    render();
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
      + '<span class="tick fdr-' + esc(fixture.difficulty || 3) + (fixture.home ? ' home' : ' away')
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

  /* --- This season, game by game ------------------------------------------

     The explorer's columns are all season-long rates, and a rate hides its own
     shape: 0.4 goals per 90 is the same number whether it is a goal every other
     week or four in one afternoon and nothing since. The log is where that
     shape lives, and it is also the only place on the page that says what a
     player did in a match that brought no goal.

     Three row states, and keeping them apart is the point:
       played   — the summary is in and the player was on the pitch
       blank    — the summary is in and says nought minutes
       partial  — no summary yet, so what was scored is known and what was
                  played is not. `needs: 'summary'` marks the columns that have
                  to sit this one out rather than print a nought nobody earned. */

  /* Which numbers a position is actually scored on. A forward has no clean
     sheet to keep and a keeper has no DefCon threshold to clear, so printing
     those columns for them would be a column of zeroes dressed up as a stat. */
  function seasonColumns(player) {
    var pos = player.position;
    var threshold = player.defcon_threshold;
    var cols = [];

    /* `keep` marks the columns that still mean something in a game the player
       watched from the bench: nought minutes for nought points really is what
       happened, whereas "0 goals" would read as a poor afternoon rather than no
       afternoon at all. */
    function col(spec) { cols.push(spec); }

    col({
      label: 'Pts', tip: 'FPL points for the match', needs: 'summary', keep: true,
      read: function (m) {
        // Six is the rough line between a week that moved your rank and one that
        // did not — a goal, or a clean sheet with a bonus point on top.
        return { text: m.points, cls: 'tot' + (m.points >= 6 ? ' on' : '') };
      },
      total: function (t) { return { text: t.points, cls: 'tot' }; }
    });

    if (pos === 'GKP') {
      col({
        label: 'SV', tip: 'Saves — 1 point per 3',
        read: function (m) { return { text: m.saves, cls: m.saves >= 3 ? 'on' : '' }; },
        total: function (t) {
          return { text: t.saves, tip: Math.floor(t.saves / 3) + ' points’ worth' };
        }
      });
    }

    col({
      label: 'G', tip: 'Goals scored',
      read: function (m) { return { text: m.goals, cls: m.goals ? 'on' : '' }; },
      total: function (t) { return { text: t.goals }; }
    });

    col({
      label: 'A', tip: 'Assists',
      read: function (m) { return { text: m.assists, cls: m.assists ? 'on' : '' }; },
      total: function (t) { return { text: t.assists }; }
    });

    if (threshold) {
      col({
        label: 'DC', needs: 'summary',
        tip: 'Defensive contributions — ' + threshold + ' in a match banks 2 points',
        read: function (m) {
          // Older rows predate the stat; a missing count is not a quiet shift.
          if (m.defcon === null || m.defcon === undefined) return { text: '—', cls: 'dim' };
          return { text: m.defcon, cls: m.defcon_hit ? 'on' : '' };
        },
        total: function (t) {
          return {
            text: t.defcon,
            tip: t.defcon_hits + ' of ' + t.defcon_chances
              + ' fetched games of 60+ minutes cleared ' + threshold
          };
        }
      });
    }

    if (pos !== 'FWD') {
      col({
        label: 'CS', tip: 'Clean sheet', needs: 'summary',
        read: function (m) {
          return m.clean_sheet ? { text: '✓', cls: 'on' } : { text: '·', cls: 'dim' };
        },
        total: function (t) { return { text: t.clean_sheets }; }
      });

      col({
        label: 'GC', needs: 'summary',
        tip: 'Goals conceded while on the pitch'
          + (pos === 'GKP' || pos === 'DEF' ? ' — −1 point per 2' : ''),
        read: function (m) {
          var costly = (pos === 'GKP' || pos === 'DEF') && m.conceded >= 2;
          return { text: m.conceded, cls: costly ? 'off' : '' };
        },
        total: function (t) { return { text: t.conceded }; }
      });
    }

    col({
      label: 'BNS', tip: 'Bonus points',
      read: function (m) { return { text: m.bonus, cls: m.bonus ? 'on' : '' }; },
      total: function (t) { return { text: t.bonus }; }
    });

    col({
      label: 'Min', tip: 'Minutes played', needs: 'summary', keep: true,
      read: function (m) {
        return { text: m.minutes + '′', cls: m.minutes >= 60 ? '' : 'dim' };
      },
      total: function (t) {
        return { text: t.minutes + '′', tip: t.starts + ' of them as a starter' };
      }
    });

    return cols;
  }

  /* Everything that is not a number: the cards and the misses that explain a
     points total which otherwise does not add up. Absent unless it happened. */
  function seasonMarks(match) {
    var out = [];
    function mark(count, cls, code, label) {
      if (!count) return;
      out.push('<span class="mk ' + cls + '" title="' + esc(label) + '">'
        + esc(code) + '</span>');
    }
    mark(match.yellow, 'yellow', 'Y', 'Yellow card');
    mark(match.red, 'red', 'R', 'Red card');
    mark(match.own_goals, 'red', 'OG', 'Own goal');
    mark(match.pens_missed, 'red', 'PM', 'Penalty missed');
    mark(match.pens_saved, 'good', 'PS', 'Penalty saved');
    return out.join('');
  }

  function seasonFixture(match) {
    return '<span class="tick fdr-' + esc(match.difficulty || 3)
      + (match.home ? ' home' : ' away') + '" title="'
      + esc(match.home ? 'Home to ' : 'Away to ') + esc(match.opp_name) + '">'
      + esc(match.opp) + '</span>';
  }

  function seasonResult(match) {
    if (!match.result) return '<span class="dim">—</span>';
    var kind = match.result === 'W' ? 'win' : (match.result === 'L' ? 'loss' : 'draw');
    return '<span class="meet ' + kind + '"><b>' + esc(match.result) + '</b> '
      + esc(match.score) + '</span>';
  }

  function renderSeason(payload) {
    var player = payload.player || {};
    var log = payload.season_log || {};
    var rows = log.rows || [];
    var totals = log.totals || {};
    var season = payload.season || 'This season';

    var title = 'This season, game by game';
    if (!rows.length) {
      return '<section class="detail-block season-block">'
        + '<h4 class="detail-title">' + esc(title)
        + '<span class="detail-note">' + esc(season) + '</span></h4>'
        + '<p class="season-empty">' + esc(player.team_name || 'This club')
        + ' have not finished a match yet this season.</p>'
        + '</section>';
    }

    var cols = seasonColumns(player);
    var head = '<tr><th>GW</th><th>Opp</th><th>Result</th>'
      + cols.map(function (c) {
          return '<th class="num" title="' + esc(c.tip) + '">' + esc(c.label) + '</th>';
        }).join('')
      + '<th title="Cards, own goals and penalties"></th></tr>';

    var waiting = 'Not fetched yet — what was scored is known, '
      + 'minutes and the rest arrive with the next refresh';

    var body = rows.map(function (match) {
      var partial = !match.loaded;
      var blank = match.loaded && match.minutes === 0;
      var context = 'GW' + match.event + ' · '
        + (match.home ? 'home to ' : 'away to ') + match.opp_name;
      var cells = cols.map(function (c) {
        // No summary: the columns that live in it have nothing honest to say.
        if (partial && c.needs === 'summary') {
          return '<td class="num" title="' + esc(context + ' · ' + waiting) + '">'
            + '<span class="dim">?</span></td>';
        }
        if (blank && !c.keep) return '<td class="num"><span class="dim">—</span></td>';
        var value = c.read(match);
        return '<td class="num" title="' + esc(context + ' · ' + c.tip) + '">'
          + '<span class="' + esc(value.cls || '') + '">' + esc(value.text) + '</span></td>';
      }).join('');
      return '<tr class="' + (blank ? 'dnp' : (partial ? 'partial' : '')) + '"'
        + (partial ? ' title="' + esc(waiting) + '"' : '') + '>'
        + '<td class="gw-cell"><span class="opp-gw">GW' + esc(match.event) + '</span></td>'
        + '<td>' + seasonFixture(match) + '</td>'
        + '<td>' + seasonResult(match) + '</td>'
        + cells
        + '<td class="gw-marks">' + seasonMarks(match) + '</td></tr>';
    }).join('');

    var foot = '<tr><td colspan="3" class="gw-total-label" title="'
      + esc('FPL’s own season figures, so the totals are right even where a '
            + 'gameweek above is still to be fetched') + '">Season</td>'
      + cols.map(function (c) {
          var value = c.total ? c.total(totals) : { text: '' };
          return '<td class="num"' + (value.tip ? ' title="' + esc(value.tip) + '"' : '')
            + '><span class="' + esc(value.cls || '') + '">' + esc(value.text) + '</span></td>';
        }).join('')
      + '<td></td></tr>';

    var note = season + ' · ' + totals.games + ' match'
      + (totals.games === 1 ? '' : 'es') + ' played';
    if (totals.missing) note += ' · ' + totals.missing + ' not fetched yet';

    return '<section class="detail-block season-block">'
      + '<h4 class="detail-title">' + esc(title)
      + '<span class="detail-note">' + esc(note) + '</span></h4>'
      + '<div class="gwlog-wrap"><table class="gwlog">'
      + '<thead>' + head + '</thead>'
      + '<tbody>' + body + '</tbody>'
      + '<tfoot>' + foot + '</tfoot>'
      + '</table></div></section>';
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
    // The season log stands on its own: a player with no fixtures left still
    // has a season behind them, and that is the half worth reading in May.
    var log = renderSeason(payload);
    if (!fixtures.length) {
      return '<div class="detail">' + log
        + '<section class="detail-block"><h4 class="detail-title">Next six opponents'
        + '<span class="detail-note">nothing projected</span></h4>'
        + '<p class="season-empty">No upcoming fixtures are projected for '
        + esc(player.name) + ' — run a refresh, or the season may be over.</p>'
        + '</section></div>';
    }
    return '<div class="detail">'
      + log
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

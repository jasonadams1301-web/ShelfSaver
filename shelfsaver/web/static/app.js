/* ShelfSaver web pages. Plain JavaScript, no libraries. Everything shown is built from text
   nodes (textContent), so file names and messages can never be read as page markup. */
(function () {
  'use strict';

  var page = document.body.getAttribute('data-page');
  var REFRESH_MS = 2000;
  var PAGE_SIZE = 100;
  var redirecting = false;

  // ---- small helpers

  function el(tag, attrs) {
    var node = document.createElement(tag);
    var rest = Array.prototype.slice.call(arguments, 2);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    });
    add(node, rest);
    return node;
  }

  function add(node, kids) {
    kids.forEach(function (kid) {
      if (Array.isArray(kid)) add(node, kid);
      else if (kid === null || kid === undefined || kid === false) return;
      else node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
    });
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fill(node) {
    clear(node);
    add(node, Array.prototype.slice.call(arguments, 1));
  }

  function byId(id) { return document.getElementById(id); }

  function baseName(path) {
    return String(path || '').split(/[\\/]/).pop();
  }

  function extension(path) {
    var name = baseName(path);
    var dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot).toLowerCase() : '';
  }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  // ---- number and time wording

  function fmtSize(bytes) {
    if (bytes === null || bytes === undefined) return '';
    if (bytes >= 1e12) return (bytes / 1e12).toFixed(1) + ' TB';
    var gb = bytes / 1e9;
    if (bytes > 0 && gb < 0.05) return '< 0.1 GB';
    return gb.toFixed(1) + ' GB';
  }

  function fmtCount(n) { return Number(n || 0).toLocaleString(); }

  function fmtLeft(seconds) {
    if (seconds === null || seconds === undefined) return '';
    if (seconds < 60) return 'less than a minute left';
    if (seconds < 5400) return 'about ' + plural(Math.round(seconds / 60), 'minute', 'minutes') + ' left';
    var hours = (seconds / 3600).toFixed(1).replace(/\.0$/, '');
    return 'about ' + hours + ' hours left';
  }

  function fmtSpan(seconds) {
    if (seconds === null || seconds === undefined || seconds < 0) return '';
    if (seconds < 60) return 'less than a minute';
    if (seconds < 5400) return plural(Math.round(seconds / 60), 'minute', 'minutes');
    return (seconds / 3600).toFixed(1).replace(/\.0$/, '') + ' hours';
  }

  function fmtDate(stamp) {
    if (!stamp) return '';
    return new Date(stamp * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    });
  }

  function fmtRate(bps) {
    if (!bps) return '';
    return (bps / 1e6).toFixed(bps >= 1e7 ? 0 : 1) + ' Mbps';
  }

  function picture(resolution) {
    var match = /^(\d+)x(\d+)$/.exec(resolution || '');
    if (!match) return '';
    var width = Number(match[1]);
    if (width >= 3400) return '4K';
    if (width >= 1800) return '1080p';
    if (width >= 1200) return '720p';
    return match[1] + ' x ' + match[2];
  }

  var STATUS_WORDS = {
    queued: 'Waiting its turn',
    working: 'Converting now',
    converted: 'Converted',
    skipped: 'Skipped',
    failed: 'Failed',
    waiting_in_use: 'Waiting, file is in use',
    missing: 'File not found',
    interrupted: 'Interrupted, will start again'
  };

  function statusWord(status) {
    return STATUS_WORDS[status] || 'Unknown';
  }

  var LANGUAGES = {
    eng: 'English', spa: 'Spanish', fra: 'French', fre: 'French', deu: 'German', ger: 'German',
    ita: 'Italian', jpn: 'Japanese', por: 'Portuguese', rus: 'Russian', kor: 'Korean',
    zho: 'Chinese', chi: 'Chinese', hin: 'Hindi', ara: 'Arabic', nld: 'Dutch', dut: 'Dutch',
    swe: 'Swedish', pol: 'Polish', tur: 'Turkish', ind: 'Indonesian', und: 'Unknown language'
  };

  function trackName(track) {
    var name = LANGUAGES[(track.language || '').toLowerCase()] || track.language || 'Unknown language';
    return track.title ? name + ' (' + track.title + ')' : name;
  }

  // ---- talking to the server

  var offlineShown = false;

  function setOffline(down) {
    if (down === offlineShown) return;
    offlineShown = down;
    var notice = byId('offline');
    if (notice) notice.hidden = !down;
  }

  function ApiError(message, status) {
    this.message = message;
    this.status = status;
  }

  // Resolves with the parsed answer. Rejects with an ApiError whose message is already plain
  // enough to show, or with an ApiError of status 0 when the server can't be reached.
  function api(method, url, body, options) {
    var init = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return fetch(url, init).then(function (response) {
      setOffline(false);
      return response.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (response.ok) return data;
        if (response.status === 401 && !(options && options.stay)) {
          if (!redirecting) {
            redirecting = true;
            window.location.href = '/login';
          }
        }
        var detail = data && typeof data.detail === 'string' ? data.detail : '';
        throw new ApiError(detail || 'Something went wrong. Please try again.', response.status);
      });
    }, function () {
      setOffline(true);
      throw new ApiError("Can't reach ShelfSaver.", 0);
    });
  }

  function messageOf(error) {
    return error && error.message ? error.message : 'Something went wrong. Please try again.';
  }

  // Runs `step` now and again every two seconds, one run at a time, whatever happens.
  function every(step) {
    function again() {
      var next = function () { window.setTimeout(again, REFRESH_MS); };
      var result;
      try { result = step(); } catch (e) { next(); return; }
      Promise.resolve(result).then(next, next);
    }
    again();
  }

  // ---- dialogs

  function openDialog(build) {
    var dialog = el('dialog');
    var finished = null;
    var done = new Promise(function (resolve) { finished = resolve; });
    dialog.addEventListener('close', function () {
      if (dialog.parentNode) dialog.parentNode.removeChild(dialog);
      finished(dialog.returnValue);
    });
    build(dialog);
    document.body.appendChild(dialog);
    dialog.showModal();
    return done;
  }

  function dialogButtons() {
    return el('div', { class: 'dialog-buttons' }, Array.prototype.slice.call(arguments));
  }

  // Resolves true only if the person clicks the confirm button.
  function confirmDialog(options) {
    return openDialog(function (dialog) {
      var cancel = el('button', {
        type: 'button', text: options.cancelLabel || 'Cancel', autofocus: true,
        onclick: function () { dialog.close('cancel'); }
      });
      var go = el('button', {
        type: 'button', text: options.confirmLabel, class: options.danger ? 'danger' : 'primary',
        onclick: function () { dialog.close('yes'); }
      });
      add(dialog, [el('h3', { text: options.title })]);
      options.lines.forEach(function (line) { add(dialog, [el('p', { text: line })]); });
      add(dialog, [dialogButtons(cancel, go)]);
    }).then(function (value) { return value === 'yes'; });
  }

  function messageDialog(title, lines) {
    return openDialog(function (dialog) {
      add(dialog, [el('h3', { text: title })]);
      lines.forEach(function (line) { add(dialog, [el('p', { text: line })]); });
      add(dialog, [dialogButtons(el('button', {
        type: 'button', class: 'primary', text: 'OK', autofocus: true,
        onclick: function () { dialog.close('ok'); }
      }))]);
    });
  }

  // Lets the person walk through the folders on this PC. Calls back with the chosen path.
  function browseFolders(startPath, onChoose) {
    openDialog(function (dialog) {
      var here = el('p', { class: 'folder-here' });
      var error = el('p', { class: 'err', role: 'alert', hidden: true });
      var list = el('ul', { class: 'folder-list' });
      var up = el('button', { type: 'button', class: 'small', text: 'Up one level' });
      var choose = el('button', { type: 'button', class: 'primary', text: 'Choose this folder' });
      var current = null;

      function show(path) {
        api('GET', '/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''))
          .then(function (data) {
            error.hidden = true;
            current = data.path;
            fill(here, data.path ? data.path : 'Choose a drive');
            choose.disabled = !data.path;
            up.hidden = !data.path;
            up.onclick = function () { show(data.parent); };
            clear(list);
            if (!data.folders.length) {
              add(list, [el('li', { class: 'empty small', text: 'No folders in here.' })]);
            }
            data.folders.forEach(function (folder) {
              add(list, [el('li', null, el('button', {
                type: 'button', text: folder.name,
                onclick: function () { show(folder.path); }
              }))]);
            });
          }, function (problem) {
            error.hidden = false;
            error.textContent = messageOf(problem);
          });
      }

      choose.addEventListener('click', function () {
        if (current) { dialog.close('chosen'); onChoose(current); }
      });
      add(dialog, [
        el('h3', { text: 'Choose a folder' }), here, up, error, list,
        dialogButtons(
          el('button', { type: 'button', text: 'Cancel', onclick: function () { dialog.close('cancel'); } }),
          choose)
      ]);
      show(startPath || null);
    });
  }

  // ---- level and library forms

  var LEVELS = [
    { value: 'low', name: 'Low', help: 'Safest picture. Saves the least.' },
    { value: 'medium', name: 'Medium', help: 'Looks great on a TV.', recommended: true },
    { value: 'high', name: 'High', help: 'Saves the most. May show on close inspection.' }
  ];

  function levelName(value) {
    var found = LEVELS.filter(function (l) { return l.value === value; })[0];
    return found ? found.name : '';
  }

  function levelPicker(groupName, selected) {
    var group = el('fieldset', { class: 'plain' }, el('legend', { text: 'How hard should it squeeze?' }));
    var grid = el('div', { class: 'levels' });
    LEVELS.forEach(function (level) {
      var title = el('strong', { text: level.name });
      var help = el('span', { class: 'muted help', text: level.help });
      var tag = level.recommended ? el('span', { class: 'tag', text: 'Recommended' }) : null;
      add(grid, [el('label', { class: 'level' },
        el('input', {
          type: 'radio', name: groupName, value: level.value,
          checked: level.value === selected
        }),
        el('span', { class: 'box' }, title, tag, help))]);
    });
    add(group, [grid]);
    group.value = function () {
      var picked = group.querySelector('input:checked');
      return picked ? picked.value : '';
    };
    return group;
  }

  function englishCheckbox(checked) {
    var box = el('input', { type: 'checkbox', id: 'english-only', checked: checked });
    var label = el('span', { text: 'Keep the default language soundtracks only' });
    api('GET', '/api/settings').then(function (settings) {
      label.textContent = 'Keep ' + settings.language_name + ' soundtracks only';
    }, function () {});
    return {
      input: box,
      row: el('div', { class: 'field-row' },
        el('label', { class: 'check', for: 'english-only' }, box, label),
        el('p', { class: 'muted small', text: "The language comes from Settings. " +
          "Dubbed movies keep all their soundtracks. " +
          "Removed soundtracks can't be recovered once the original is replaced." }))
    };
  }

  function commentaryCheckbox(checked) {
    var box = el('input', { type: 'checkbox', id: 'remove-commentary', checked: checked });
    return {
      input: box,
      row: el('div', { class: 'field-row' },
        el('label', { class: 'check', for: 'remove-commentary' }, box,
          el('span', { text: 'Remove commentary soundtracks' })),
        el('p', { class: 'muted small', text: "Removes soundtracks that are marked or titled as " +
          "commentary, such as a director's commentary. The main soundtrack is never removed. " +
          "Removed soundtracks can't be recovered once the original is replaced." }))
    };
  }

  function shrinkCheckbox(checked) {
    var box = el('input', { type: 'checkbox', id: 'shrink-h265', checked: checked });
    return {
      input: box,
      row: el('div', { class: 'field-row' },
        el('label', { class: 'check', for: 'shrink-h265' }, box,
          el('span', { text: 'Shrink oversized H.265 files' })),
        el('p', { class: 'muted small', text: 'Tests a short sample first and only converts ' +
          'when the file would come out at least 25% smaller. HDR files are left as they are.' }))
    };
  }

  function addLibraryDialog(done) {
    openDialog(function (dialog) {
      var name = el('input', { type: 'text', id: 'lib-name', autocomplete: 'off' });
      var folder = el('input', { type: 'text', id: 'lib-path', autocomplete: 'off', class: 'mono' });
      var levels = levelPicker('level', 'medium');
      var english = englishCheckbox(false);
      var commentary = commentaryCheckbox(false);
      var shrink = shrinkCheckbox(false);
      var error = el('p', { class: 'err', role: 'alert', hidden: true });
      var submit = el('button', { type: 'submit', class: 'primary', text: 'Add library' });
      var form = el('form', { novalidate: true },
        el('h3', { text: 'Add a library' }),
        el('div', { class: 'field-row' }, el('label', { class: 'field', for: 'lib-name', text: 'Name' }), name),
        el('div', { class: 'field-row' },
          el('label', { class: 'field', for: 'lib-path', text: 'Folder' }),
          el('div', { class: 'with-button' }, folder, el('button', {
            type: 'button', text: 'Browse',
            onclick: function () {
              browseFolders(folder.value.trim() || null, function (path) {
                folder.value = path;
                if (!name.value.trim()) name.value = baseName(path);
              });
            }
          }))),
        levels, english.row, commentary.row, shrink.row, error,
        dialogButtons(
          el('button', { type: 'button', text: 'Cancel', onclick: function () { dialog.close('cancel'); } }),
          submit));
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        submit.disabled = true;
        api('POST', '/api/libraries', {
          name: name.value, path: folder.value, level: levels.value(),
          english_only: english.input.checked, shrink_h265: shrink.input.checked,
          remove_commentary: commentary.input.checked
        }).then(function () {
          dialog.close('added');
          done();
        }, function (problem) {
          error.hidden = false;
          error.textContent = messageOf(problem);
          submit.disabled = false;
        });
      });
      dialog.appendChild(form);
    });
  }

  // ---- history table, used on Home and on a library's page

  var IGNORED_REASON = 'Ignored, left as is';
  var HISTORY_TABS = {
    conversions: { label: 'Conversion history', kind: 'run', empty: 'Nothing has been converted yet.' },
    scans: { label: 'Scan history', kind: 'file',
      empty: 'Nothing here yet. Files ShelfSaver looks at and leaves alone will show up here.' }
  };

  function createHistory(container, options) {
    var state = {
      limit: PAGE_SIZE, openId: null, rows: [], details: {}, filter: 'conversions',
      signature: ''
    };
    var tbody = el('tbody');
    var more = el('p', { class: 'empty', hidden: true });
    var empty = el('div', { class: 'empty', hidden: true, text: HISTORY_TABS[state.filter].empty });
    var table = el('table', null,
      el('thead', null, el('tr', null,
        el('th', { scope: 'col', text: 'File' }), el('th', { scope: 'col', class: 'hide-s', text: 'Was' }),
        el('th', { scope: 'col', text: 'Now' }), el('th', { scope: 'col', text: 'Saved' }))),
      tbody);
    add(container, [el('div', { class: 'table-wrap' }, table, empty), more]);

    function visible() {
      var kind = HISTORY_TABS[state.filter].kind;
      return state.rows.filter(function (row) { return row.kind === kind; });
    }

    function savedPercent(row) {
      if (!row.was.size || row.now.size === null || row.now.size === undefined) return null;
      return Math.max(0, Math.round((1 - row.now.size / row.was.size) * 100));
    }

    function sized(size, format) {
      return [size === null || size === undefined ? '' : fmtSize(size),
        format ? el('span', { class: 'sub', text: format }) : null];
    }

    function nowCell(row) {
      if (row.status === 'converted') {
        var parts = sized(row.now.size, row.now.format);
        var was = extension(row.path);
        var now = extension(row.now_path);
        if (was && now && was !== now) {
          parts.push(el('span', { class: 'sub', text: 'Renamed ' + was + ' to ' + now }));
        }
        return el('td', { class: 'num' }, parts);
      }
      var bad = row.status === 'failed';
      var word = row.status === 'interrupted' ? 'Interrupted' : statusWord(row.status);
      return el('td', { class: (bad ? 'fail' : 'skip') + ' num', text: word });
    }

    function savedCell(row) {
      if (row.status === 'converted') {
        var pct = savedPercent(row);
        return el('td', { class: 'num' },
          el('span', { class: 'save', text: pct === null ? 'Saved' : pct + '%' }),
          row.saved_bytes > 0 ? el('span', { class: 'sub', text: fmtSize(row.saved_bytes) + ' freed' }) : null);
      }
      var text = '';
      if (row.status === 'skipped') text = row.reason || 'Not needed';
      else if (row.status === 'failed') text = row.kind === 'file' ? row.reason || 'Left as is' : 'Original kept';
      else if (row.status === 'interrupted') text = 'Will start again';
      else if (row.status === 'waiting_in_use') text = 'Will try again';
      else if (row.status === 'missing') text = 'No longer there';
      return el('td', { class: row.status === 'failed' ? 'fail' : 'skip', text: text });
    }

    function summaryRow(row) {
      var open = state.openId === row.id;
      var tr = el('tr', {
        class: 'row', tabindex: 0, role: 'button', 'aria-expanded': open ? 'true' : 'false',
        'data-focus': 'row-' + row.id
      },
        el('td', { class: 'name' }, el('span', { class: 'caret', text: open ? '▾' : '▸' }),
          row.name || '(unnamed)',
          options.showLibrary && row.library ? el('span', { class: 'sub', text: row.library }) : null,
          el('span', { class: 'sub only-s', text: 'Was ' + fmtSize(row.was.size) + (row.was.format ? ' · ' + row.was.format : '') })),
        el('td', { class: 'num hide-s' }, sized(row.was.size, row.was.format)),
        nowCell(row),
        savedCell(row));
      function toggle() {
        state.openId = open ? null : row.id;
        render(true);
        if (!open) loadDetails(row, true).then(function () { render(false); });
      }
      tr.addEventListener('click', toggle);
      tr.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
      });
      return tr;
    }

    function field(label, content, wide) {
      return el('div', wide ? { class: 'wide' } : null, el('b', { text: label }), content);
    }

    function detailRow(row) {
      var cell = el('td', { colspan: 4 });
      var info = state.details[row.file_id];
      if (!info) {
        add(cell, [el('span', { class: 'muted', text: 'Loading details…' })]);
        return el('tr', { class: 'detail' }, cell);
      }
      if (info.error) {
        add(cell, [el('span', { class: 'err', text: info.error })]);
        return el('tr', { class: 'detail' }, cell);
      }
      var file = info.data.file;
      var runs = info.data.runs;
      var run = runs.filter(function (r) { return r.id === row.id; })[0] || runs[0] || {};
      var grid = el('div', { class: 'grid' });
      var why = run.why || '';
      var hdr = run.tracks && run.tracks.hdr ? ', HDR kept' : '';
      var pic = picture(file.resolution);
      if (pic) add(grid, [field('Picture', pic + hdr)]);
      if (row.was.bitrate || row.now.bitrate) {
        var rate = fmtRate(row.was.bitrate) +
          (row.now.bitrate ? ' → ' + fmtRate(row.now.bitrate) : '');
        add(grid, [field('Data rate', rate)]);
      }
      var tracks = run.tracks;
      if (tracks && tracks.kept) {
        var keptAudio = tracks.kept.filter(function (t) { return t.kind === 'audio'; });
        var goneAudio = (tracks.removed || []).filter(function (t) { return t.kind === 'audio'; });
        var keptSubs = tracks.kept.filter(function (t) { return t.kind === 'subtitle'; });
        var soundText = keptAudio.length + ' of ' + (keptAudio.length + goneAudio.length) + ' kept' +
          (keptAudio.length ? ' (' + keptAudio.map(trackName).join(', ') + ')' : '');
        add(grid, [field('Soundtracks', [soundText,
          goneAudio.length ? el('span', { class: 'sub', text: 'Removed: ' + goneAudio.map(trackName).join(', ') }) : null])]);
        add(grid, [field('Subtitles', keptSubs.length ? plural(keptSubs.length, 'subtitle', 'subtitles') + ' kept' : 'None')]);
      }
      if (run.convert_seconds !== null && run.convert_seconds !== undefined && row.status === 'converted') {
        add(grid, [field('Conversion time', fmtSpan(run.convert_seconds))]);
      }
      if (run.finished_at) add(grid, [field('Finished', fmtDate(run.finished_at))]);
      if (run.checks) {
        var labels = [['length', 'Length'], ['tracks', 'Tracks'], ['plays', 'Plays through'], ['smaller', 'Smaller']];
        var marks = el('div', { class: 'checks' });
        labels.forEach(function (pair) {
          if (!(pair[0] in run.checks)) return;
          var good = run.checks[pair[0]];
          add(marks, [el('span', { class: good ? 'ok' : 'fail', text: (good ? '✓ ' : '✗ ') + pair[1] })]);
        });
        add(grid, [field('Checks', marks, true)]);
      }
      if (row.status === 'skipped') {
        if (row.reason) add(grid, [field('Why it was skipped', row.reason, true)]);
        // A run's row already shows when it finished, so this is only for files that never ran.
        if (row.kind === 'file' && row.updated_at) {
          add(grid, [field(row.reason === IGNORED_REASON ? 'Ignored' : 'Scanned', fmtDate(row.updated_at), true)]);
        }
      } else if (why) {
        add(grid, [field('Why it was converted', why, true)]);
      }
      var happened = row.status === 'converted' || row.status === 'skipped' ? '' : (row.reason || run.error || '');
      if (!happened && row.status === 'missing') happened = "ShelfSaver can't find this file in its folder any more.";
      if (happened) add(grid, [field('What happened', happened, true)]);
      if (run.state === 'failed' && run.log_tail) {
        add(grid, [field('What FFmpeg said', el('pre', { class: 'run-log', text: run.log_tail }), true)]);
      }
      add(grid, [field('Where it is', el('span', { class: 'mono small', text: file.path }), true)]);
      add(cell, [grid]);

      var actions = el('div', { class: 'detail-actions' });
      var error = el('p', { class: 'err', role: 'alert', hidden: true });
      function report(problem) {
        error.hidden = false;
        error.textContent = messageOf(problem);
      }
      var needsAttention = file.status === 'failed' && runs.some(function (r) { return r.state === 'swapping'; });
      if (file.status === 'failed' || file.status === 'waiting_in_use') {
        add(actions, [el('button', {
          type: 'button', class: 'primary small', text: 'Try again', 'data-focus': 'retry',
          onclick: function () {
            api('POST', '/api/files/' + file.id + '/retry').then(function () { refresh(); }, report);
          }
        })]);
      }
      if (file.status === 'skipped' && file.reason === IGNORED_REASON) {
        add(actions, [el('button', {
          type: 'button', class: 'small', text: 'Stop ignoring', 'data-focus': 'retry',
          title: 'Put this file back in the queue',
          onclick: function () {
            api('POST', '/api/files/' + file.id + '/retry').then(function () { refresh(); }, report);
          }
        })]);
      }
      if (file.status === 'failed' && !needsAttention) {
        add(actions, [el('button', {
          type: 'button', class: 'small', text: 'Ignore', 'data-focus': 'ignore',
          title: 'Keep this file as it is, and skip it unless a new copy replaces it',
          onclick: function () {
            api('POST', '/api/files/' + file.id + '/ignore').then(function () { refresh(); }, report);
          }
        })]);
        add(actions, [el('button', {
          type: 'button', class: 'small danger', text: 'Delete file', 'data-focus': 'delete',
          onclick: function () {
            confirmDialog({
              title: 'Delete this file for good?',
              lines: [
                file.path,
                fmtSize(file.size) + (file.reason ? ' · ' + file.reason : ''),
                'The file will be permanently deleted from the drive. There is no Recycle Bin and ' +
                'ShelfSaver can\'t bring it back.'
              ],
              confirmLabel: 'Delete file', danger: true
            }).then(function (yes) {
              if (!yes) return;
              api('POST', '/api/files/' + file.id + '/delete', { confirm: true })
                .then(function () { refresh(); }, report);
            });
          }
        })]);
      }
      if (needsAttention) {
        add(actions, [el('button', {
          type: 'button', class: 'small', text: "I've dealt with this", 'data-focus': 'resolve',
          onclick: function () {
            confirmDialog({
              title: 'Close this file as handled?',
              lines: [
                row.reason || run.error || "ShelfSaver couldn't finish replacing this file.",
                'Only continue once you have checked the leftover files in the ShelfSaver folder on this drive. ' +
                'Nothing will be deleted or moved by this.'
              ],
              confirmLabel: 'Yes, I have dealt with it'
            }).then(function (yes) {
              if (!yes) return;
              api('POST', '/api/files/' + file.id + '/resolve', { confirm: true })
                .then(function () { refresh(); }, report);
            });
          }
        })]);
      }
      if (actions.firstChild) add(cell, [actions]);
      add(cell, [error]);
      return el('tr', { class: 'detail' }, cell);
    }

    function render(force) {
      var rows = visible();
      var openInfo = state.openId !== null ? JSON.stringify(state.details) : '';
      var signature = JSON.stringify([rows, state.openId, openInfo, state.filter, state.limit]);
      if (!force && signature === state.signature) return;
      state.signature = signature;
      var active = document.activeElement;
      var focusKey = active && active.getAttribute ? active.getAttribute('data-focus') : null;
      var scrollY = window.scrollY;
      clear(tbody);
      rows.forEach(function (row) {
        add(tbody, [summaryRow(row)]);
        if (state.openId === row.id) add(tbody, [detailRow(row)]);
      });
      if (state.openId !== null && !rows.some(function (r) { return r.id === state.openId; })) state.openId = null;
      empty.hidden = rows.length > 0;
      table.querySelector('thead').hidden = rows.length === 0;
      if (state.rows.length >= state.limit) {
        more.hidden = false;
        fill(more, el('button', {
          type: 'button', class: 'small', text: 'Show more',
          onclick: function () { state.limit += PAGE_SIZE; refresh(); }
        }));
      } else {
        more.hidden = true;
      }
      if (focusKey) {
        var back = container.querySelector('[data-focus="' + focusKey + '"]');
        if (back) back.focus({ preventScroll: true });
      }
      window.scrollTo(window.scrollX, scrollY);
    }

    function loadDetails(row, showLoading) {
      if (showLoading) delete state.details[row.file_id];
      return api('GET', '/api/files/' + row.file_id).then(function (data) {
        state.details[row.file_id] = { data: data };
      }, function (problem) {
        if (problem.status !== 0) state.details[row.file_id] = { error: messageOf(problem) };
      });
    }

    function refresh() {
      var query = '?limit=' + state.limit + (options.libraryId ? '&library_id=' + options.libraryId : '') +
        '&show=' + state.filter;
      return api('GET', '/api/history' + query).then(function (rows) {
        state.rows = rows;
        var open = rows.filter(function (r) { return r.id === state.openId; })[0];
        return open ? loadDetails(open, false) : null;
      }).then(function () { render(false); }, function () { /* the offline notice says it */ });
    }

    return {
      refresh: refresh,
      setFilter: function (value) {
        state.filter = value;
        state.limit = PAGE_SIZE;
        empty.textContent = HISTORY_TABS[value].empty;
        render(true);
        return refresh();
      }
    };
  }

  function drawHistoryTabs(box, history) {
    var chosen = 'conversions';
    function draw() {
      clear(box);
      Object.keys(HISTORY_TABS).forEach(function (key) {
        add(box, [el('button', {
          type: 'button', class: 'small', text: HISTORY_TABS[key].label, 'aria-pressed': chosen === key ? 'true' : 'false',
          onclick: function () { chosen = key; draw(); history.setFilter(key); }
        })]);
      });
    }
    draw();
  }

  // ---- notices

  function notice(kind, content) {
    return el('div', { class: 'notice ' + (kind || '') }, content);
  }

  function showNotices(container, status) {
    var items = [];
    var worker = status.worker || {};
    var message = status.message;
    if (status.no_scratch && message === 'Choose a scratch disk in Settings') message = null;
    if (message) {
      items.push(notice('', [message,
        worker.state === 'no_scratch' ? [' ', el('a', { href: '/settings', text: 'Open Settings' })] : null]));
    }
    (status.offline_libraries || []).forEach(function (lib) {
      items.push(notice('', 'The library drive is offline for "' + lib.name + '". Check that it is connected. Other libraries carry on.'));
    });
    var signature = JSON.stringify(items.map(function (n) { return n.textContent; }));
    if (container.getAttribute('data-sig') === signature) return;
    container.setAttribute('data-sig', signature);
    fill(container, items);
  }

  // ---- Home

  var DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var PAUSE_HELP = 'Stops converting. The file being converted will start over when you resume.';

  function basenameOfStatus(worker) { return baseName(worker.file); }

  function scanLine(scanner) {
    if (!scanner) return null;
    if (scanner.pending > 0) {
      return 'Looking through your libraries: ' + fmtCount(scanner.pending) +
        (scanner.pending === 1 ? ' file' : ' files') + ' to check';
    }
    return scanner.scanning ? 'Looking through your libraries…' : null;
  }

  function fmtClock(date) {
    return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  // Comes in the PC's own local time, and is read as local here so the clock digits stay the same.
  function fmtBlackoutEnd(until) {
    var end = new Date(until);
    var today = end.toDateString() === new Date().toDateString();
    return (today ? '' : DAY_NAMES[(end.getDay() + 6) % 7] + ' ') + fmtClock(end);
  }

  function drawNow(box, status) {
    var worker = status.worker || {};
    var title, line = null, bar = null;
    var scanning = scanLine(status.scanner);
    if (worker.state === 'working' && worker.file) {
      title = (worker.step || 'Working') + ': ' + basenameOfStatus(worker);
      var known = worker.percent !== null && worker.percent !== undefined;
      bar = el('div', { class: 'bar' + (known ? '' : ' unknown'), role: 'progressbar',
        'aria-label': 'Progress', 'aria-valuemin': 0, 'aria-valuemax': 100,
        'aria-valuenow': known ? Math.round(worker.percent) : null },
        el('i', { style: known ? 'width:' + Math.min(100, worker.percent) + '%' : null }));
      line = known ? Math.round(worker.percent) + '%' +
        (worker.eta_seconds !== null && worker.eta_seconds !== undefined ? ' · ' + fmtLeft(worker.eta_seconds) : '')
        : 'Getting started…';
    } else if (worker.state === 'paused') {
      title = 'Paused';
      line = 'Nothing will be converted until you press Resume. A file that was being converted will start over when you resume.';
    } else if (worker.state === 'blackout') {
      title = 'Blackout until ' + fmtBlackoutEnd(worker.blackout_until);
      line = 'No new files start during a blackout. You can change the times in Settings.';
    } else if (worker.state === 'no_scratch') {
      title = 'Waiting for a scratch disk';
      line = 'Conversions start once a scratch disk is available.';
    } else {
      title = 'Nothing is being converted right now';
      if (status.stats && status.stats.remaining) {
        line = 'The next file will start shortly.';
      } else {
        line = scanning || 'ShelfSaver is watching your libraries for new files.';
        scanning = null;
      }
    }
    var signature = JSON.stringify([title, line, scanning, worker.percent]);
    if (box.getAttribute('data-sig') === signature) return;
    box.setAttribute('data-sig', signature);
    fill(box, el('div', { class: 'now-title', text: title }), bar, el('div', { class: 'muted small', text: line }),
      scanning ? el('div', { class: 'muted small', text: scanning }) : null);
  }

  function initHome() {
    var history = createHistory(byId('history'), { showLibrary: true });
    drawHistoryTabs(byId('filters'), history);
    var pause = byId('pause');
    var paused = false;

    pause.addEventListener('click', function () {
      pause.disabled = true;
      api('POST', paused ? '/api/resume' : '/api/pause').then(function () { return refresh(); }, function () {})
        .then(function () { pause.disabled = false; });
    });

    function drawLibraries(libs) {
      var signature = JSON.stringify(libs.map(function (l) { return [l.id, l.name, l.status]; }));
      var box = byId('libs');
      if (box.getAttribute('data-sig') === signature) return;
      box.setAttribute('data-sig', signature);
      clear(box);
      libs.forEach(function (lib) {
        add(box, [el('a', { class: 'lib-link', href: '/library?id=' + lib.id },
          lib.name, lib.status === 'offline' ? el('span', { class: 'tag off', text: 'Offline' }) : null)]);
      });
      add(box, [el('button', { type: 'button', class: 'primary small', text: 'Add a library',
        onclick: function () { addLibraryDialog(function () { refresh(); }); } })]);
    }

    function refresh() {
      return Promise.all([api('GET', '/api/status'), api('GET', '/api/libraries')]).then(function (all) {
        var status = all[0];
        paused = (status.worker || {}).state === 'paused';
        pause.hidden = false;
        pause.textContent = paused ? 'Resume' : 'Pause';
        pause.title = paused ? 'Start converting again' : PAUSE_HELP;
        byId('first-run').hidden = !status.no_scratch;
        byId('stat-saved').textContent = fmtSize(status.stats.saved_bytes);
        byId('stat-converted').textContent = fmtCount(status.stats.converted);
        byId('stat-remaining').textContent = fmtCount(status.stats.remaining);
        showNotices(byId('message'), status);
        drawNow(byId('now'), status);
        drawLibraries(all[1]);
        return history.refresh();
      }, function () { /* offline: the notice is already showing */ });
    }

    function drawUse(use) {
      ['cpu', 'gpu', 'ram'].forEach(function (name) {
        var value = use[name];
        byId('stat-' + name).textContent = value === null || value === undefined ? '–' : value + '%';
      });
    }

    function refreshUse() {
      return api('GET', '/api/system').then(drawUse, function () { drawUse({}); });
    }

    every(refresh);
    every(refreshUse);
  }

  // ---- The queue

  var QUEUE_MOST = 500;

  function initQueue() {
    var limit = PAGE_SIZE;
    var signature = '';
    var tbody = el('tbody');
    var empty = el('div', { class: 'empty', hidden: true, text: 'Nothing is waiting to be converted.' });
    var table = el('table', null,
      el('thead', null, el('tr', null,
        el('th', { scope: 'col', class: 'place', text: '#' }), el('th', { scope: 'col', text: 'File' }),
        el('th', { scope: 'col', text: 'Size' }))),
      tbody);
    var more = el('p', { class: 'empty', hidden: true });
    add(byId('queue'), [el('div', { class: 'table-wrap' }, table, empty), more]);

    function draw(queue) {
      var seen = JSON.stringify([queue, limit]);
      if (seen === signature) return;
      signature = seen;
      byId('queue-summary').textContent = queue.total_files
        ? fmtCount(queue.total_files) + (queue.total_files === 1 ? ' file' : ' files') + ' waiting · ' +
          fmtSize(queue.total_bytes) + ' · The largest files go first.'
        : '';
      table.hidden = !queue.items.length;
      empty.hidden = !!queue.items.length;
      fill(tbody, queue.items.map(function (row, index) {
        return el('tr', null,
          el('td', { class: 'num', text: fmtCount(index + 1) }),
          el('td', { class: 'name' }, row.name, el('span', { class: 'sub', text: row.library })),
          el('td', { class: 'num' }, fmtSize(row.size),
            row.resolution ? el('span', { class: 'sub', text: row.resolution }) : null));
      }));
      var hidden = queue.total_files - queue.items.length;
      more.hidden = hidden <= 0;
      if (hidden > 0 && limit < QUEUE_MOST) {
        fill(more, el('button', {
          type: 'button', class: 'small', text: 'Show more',
          onclick: function () { limit = Math.min(limit + PAGE_SIZE, QUEUE_MOST); refresh(); }
        }));
      } else if (hidden > 0) {
        more.textContent = 'Showing the next ' + fmtCount(queue.items.length) + ' files.';
      }
    }

    function refresh() {
      return Promise.all([api('GET', '/api/status'), api('GET', '/api/queue?limit=' + limit)]).then(function (all) {
        showNotices(byId('message'), all[0]);
        drawNow(byId('now'), all[0]);
        draw(all[1]);
      }, function () { /* offline: the notice is already showing */ });
    }

    every(refresh);
  }

  // ---- A library

  function initLibrary() {
    var id = Number((new URLSearchParams(window.location.search)).get('id'));
    var head = byId('library-head');
    var history = createHistory(byId('history'), { libraryId: id, showLibrary: false });
    drawHistoryTabs(byId('filters'), history);
    var current = null;

    function openOptions(lib) {
      openDialog(function (dialog) {
        var levels = levelPicker('level', lib.level);
        var english = englishCheckbox(lib.english_only);
        var commentary = commentaryCheckbox(lib.remove_commentary);
        var shrink = shrinkCheckbox(lib.shrink_h265);
        var error = el('p', { class: 'err', role: 'alert', hidden: true });
        var save = el('button', { type: 'submit', class: 'primary', text: 'Save' });
        var form = el('form', { novalidate: true },
          el('h3', { text: 'Library options' }), levels, english.row, commentary.row, shrink.row,
          el('p', { class: 'muted small', text: 'Changes apply to files that are not converted yet.' }),
          error,
          dialogButtons(
            el('button', { type: 'button', class: 'danger', text: 'Remove library', onclick: function () {
              dialog.close('remove');
              confirmDialog({
                title: 'Remove "' + lib.name + '"?',
                lines: ['ShelfSaver will stop watching this folder. Your files stay exactly where they are.'],
                confirmLabel: 'Remove library', danger: true
              }).then(function (yes) {
                if (!yes) return;
                api('DELETE', '/api/libraries/' + lib.id).then(function () {
                  window.location.href = '/';
                }, function (problem) { messageDialog("Couldn't remove it", [messageOf(problem)]); });
              });
            } }),
            el('button', { type: 'button', text: 'Cancel', onclick: function () { dialog.close('cancel'); } }),
            save));
        form.addEventListener('submit', function (event) {
          event.preventDefault();
          save.disabled = true;
          api('PATCH', '/api/libraries/' + lib.id, {
            level: levels.value(), english_only: english.input.checked,
            shrink_h265: shrink.input.checked, remove_commentary: commentary.input.checked
          })
            .then(function () { dialog.close('saved'); refresh(); }, function (problem) {
              error.hidden = false;
              error.textContent = messageOf(problem);
              save.disabled = false;
            });
        });
        dialog.appendChild(form);
      });
    }

    function drawHead(lib) {
      var signature = JSON.stringify(lib);
      if (head.getAttribute('data-sig') === signature) return;
      head.setAttribute('data-sig', signature);
      fill(head,
        el('div', null,
          el('h1', { text: lib.name }),
          el('div', { class: 'muted small path' }, lib.path + ' · ',
            lib.status === 'offline' ? el('strong', { text: 'Offline' }) : 'Watching for new files'),
          el('div', { class: 'muted small', text: fmtCount(lib.stats.total) + ' total · ' +
            fmtSize(lib.stats.saved_bytes) + ' saved · ' +
            plural(lib.stats.converted, 'file', 'files') + ' converted · ' + lib.stats.remaining + ' still to do' })),
        el('div', { class: 'libs' },
          el('span', { class: 'pill', text: levelName(lib.level) }),
          el('button', { type: 'button', class: 'small', text: 'Options', onclick: function () { openOptions(lib); } })));
    }

    function refresh() {
      return Promise.all([api('GET', '/api/libraries'), api('GET', '/api/status')]).then(function (all) {
        current = all[0].filter(function (l) { return l.id === id; })[0];
        if (!current) {
          fill(head, el('div', null, el('h1', { text: 'Library not found' }),
            el('p', { class: 'muted' }, 'That library is gone. ', el('a', { href: '/', text: 'Back to Home' }))));
          return null;
        }
        document.title = current.name + ' - ShelfSaver';
        drawHead(current);
        showNotices(byId('message'), { message: all[1].message, worker: all[1].worker, offline_libraries: [] });
        return history.refresh();
      }, function () { /* offline */ });
    }

    every(refresh);
  }

  // ---- Settings

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var box = el('textarea', { readonly: true, style: 'position:fixed;left:-1000px;top:0' });
      box.value = text;
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(box);
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  function copyBlock(label, text) {
    var status = el('span', { class: 'small', role: 'status' });
    var button = el('button', {
      type: 'button', class: 'small', text: 'Copy',
      onclick: function () {
        copyText(text).then(function () { status.textContent = ' Copied'; },
          function () { status.textContent = " Couldn't copy. Select the text and copy it yourself."; });
      }
    });
    return el('div', { class: 'field-row' },
      el('p', { class: 'small', text: label }),
      el('pre', { class: 'mono', text: text }), button, status);
  }

  function initSettings() {
    var box = byId('settings');
    var trouble = byId('trouble-body');
    var data = null;

    function clockText(value) {
      var parts = value.split(':');
      return fmtClock(new Date(2000, 0, 1, Number(parts[0]), Number(parts[1])));
    }

    function daysText(days) {
      if (days.length === 7) return 'Every day';
      if (days.join() === '0,1,2,3,4') return 'Weekdays';
      if (days.join() === '5,6') return 'Weekends';
      return days.map(function (d) { return DAY_NAMES[d]; }).join(', ');
    }

    function blackoutText(blackout) {
      var overnight = blackout.end < blackout.start ? ' (next day)' : '';
      return daysText(blackout.days) + ', ' + clockText(blackout.start) + ' to ' +
        clockText(blackout.end) + overnight;
    }

    function drawSchedule(holder) {
      var note = el('p', { class: 'small', role: 'status' });
      function save(blackouts) {
        api('PATCH', '/api/settings', { blackouts: blackouts }).then(function (saved) {
          data.blackouts = saved.blackouts;
          drawSchedule(holder);
        }, function (problem) {
          note.className = 'small err';
          note.textContent = messageOf(problem);
        });
      }

      var rows = data.blackouts.map(function (blackout, index) {
        return el('li', { class: 'blackout' }, el('span', { text: blackoutText(blackout) }),
          el('button', { type: 'button', class: 'small', text: 'Remove', onclick: function () {
            save(data.blackouts.filter(function (b, i) { return i !== index; }));
          } }));
      });

      var start = el('input', { type: 'time', id: 'blackout-start', value: '18:00' });
      var end = el('input', { type: 'time', id: 'blackout-end', value: '23:00' });
      var dayBoxes = DAY_NAMES.map(function (name, day) {
        return el('input', { type: 'checkbox', id: 'blackout-day-' + day, value: day, checked: true });
      });

      fill(holder,
        el('h3', { text: 'Schedule' }),
        el('p', { class: 'muted small', text: 'ShelfSaver runs 24/7 unless you add a blackout. No new files start during a blackout, and a file already being converted finishes first. Times follow the clock on the PC running ShelfSaver.' }),
        rows.length ? el('ul', { class: 'blackouts' }, rows)
          : el('p', { class: 'small', text: 'Runs 24/7' }),
        el('div', { class: 'blackout-times' },
          el('div', null, el('label', { class: 'field', for: 'blackout-start', text: 'From' }), start),
          el('div', null, el('label', { class: 'field', for: 'blackout-end', text: 'To' }), end)),
        el('fieldset', { class: 'blackout-days' }, el('legend', { class: 'field', text: 'Days' }),
          dayBoxes.map(function (box, day) {
            return el('label', { for: box.id }, box, el('span', { text: DAY_NAMES[day] }));
          })),
        el('button', { type: 'button', text: 'Add a blackout', onclick: function () {
          var days = dayBoxes.filter(function (box) { return box.checked; })
            .map(function (box) { return Number(box.value); });
          save(data.blackouts.concat([{ days: days, start: start.value, end: end.value }]));
        } }),
        note);
    }

    function draw() {
      var scratch = el('input', { type: 'text', id: 'scratch', class: 'mono', autocomplete: 'off',
        value: data.scratch_dir || '' });
      var scratchNote = el('p', { class: 'small', role: 'status' });
      function saveScratch(path) {
        api('PATCH', '/api/settings', { scratch_dir: path }).then(function (saved) {
          scratch.value = saved.scratch_dir || path;
          scratchNote.className = 'small ok';
          scratchNote.textContent = 'Saved. ShelfSaver will use this folder for its work.';
        }, function (problem) {
          scratchNote.className = 'small err';
          scratchNote.textContent = messageOf(problem);
        });
      }

      var holdNote = el('p', { class: 'small', role: 'status' });
      var holdBox = el('input', { type: 'checkbox', id: 'holding', checked: data.holding_enabled });
      holdBox.addEventListener('change', function () {
        api('PATCH', '/api/settings', { holding_enabled: holdBox.checked }).then(function (saved) {
          holdBox.checked = saved.holding_enabled;
          holdNote.className = 'small ok';
          holdNote.textContent = saved.holding_enabled ? 'Originals will be held after replacing.'
            : 'Originals will be removed as soon as the new file is checked.';
        }, function (problem) {
          holdBox.checked = !holdBox.checked;
          holdNote.className = 'small err';
          holdNote.textContent = messageOf(problem);
        });
      });

      var clearBtn = el('button', { type: 'button', text: 'Clear the holding folder', onclick: function () {
        var drives = data.held_drives.length ? data.held_drives.join(', ') : 'the drives your libraries are on';
        var names = data.held_libraries.length ? ' Libraries affected: ' + data.held_libraries.join(', ') + '.' : '';
        confirmDialog({
          title: 'Clear the holding folder?',
          lines: [
            'This clears the held originals for every library on the same drive, not just one: ' +
              'drives affected: ' + drives + '.' + names,
            'The held originals (' + fmtSize(data.held_bytes) + ') are deleted permanently. ' +
              "They can't be brought back.",
            'ShelfSaver can only do this when no file is being replaced.'
          ],
          confirmLabel: 'Delete the held originals', danger: true
        }).then(function (yes) {
          if (!yes) return;
          api('POST', '/api/holding/clear', { confirm: true }).then(function (result) {
            holdNote.className = 'small ok';
            holdNote.textContent = 'Cleared ' + fmtSize(result.freed_bytes) + ' on ' +
              (result.drives.length ? result.drives.join(', ') : 'your drives') + '.';
            load();
          }, function (problem) {
            holdNote.className = 'small err';
            holdNote.textContent = messageOf(problem);
          });
        });
      } });

      var schedule = el('div', { class: 'setting' });
      drawSchedule(schedule);

      var languageNote = el('p', { class: 'small', role: 'status' });
      var language = el('select', { id: 'default-language' }, data.languages.map(function (choice) {
        return el('option', { value: choice.code, text: choice.name });
      }));
      language.value = data.default_language;
      function saveLanguage() {
        api('PATCH', '/api/settings', { default_language: language.value }).then(function (saved) {
          data.default_language = saved.default_language;
          languageNote.className = 'small ok';
          languageNote.textContent = 'Saved. Libraries that remove soundtracks will keep ' + saved.language_name + '.';
        }, function (problem) {
          language.value = data.default_language;
          languageNote.className = 'small err';
          languageNote.textContent = messageOf(problem);
        });
      }
      language.addEventListener('change', function () {
        if (!data.language_libraries.length) { saveLanguage(); return; }
        var name = language.options[language.selectedIndex].text;
        confirmDialog({
          title: 'Keep ' + name + ' soundtracks?',
          lines: [
            'These libraries remove soundtracks in other languages, so every file in them will be checked again: ' +
              data.language_libraries.join(', ') + '.',
            'Files where ' + name + ' is the first and default soundtrack can lose their other soundtracks. ' +
              "Removed soundtracks can't be recovered once the original is replaced. " +
              "Soundtracks that were already removed don't come back."
          ],
          confirmLabel: 'Change the language', danger: true
        }).then(function (yes) {
          if (yes) saveLanguage();
          else language.value = data.default_language;
        });
      });

      fill(box,
        el('div', { class: 'setting' },
          el('h3', { text: 'Scratch disk' }),
          el('p', { class: 'muted small', text: 'A drive with plenty of free space where ShelfSaver works on one file at a time. It must not be inside a library.' }),
          el('label', { class: 'field', for: 'scratch', text: 'Folder' }),
          el('div', { class: 'with-button' }, scratch,
            el('button', { type: 'button', text: 'Browse', onclick: function () {
              browseFolders(scratch.value.trim() || null, function (path) { scratch.value = path; saveScratch(path); });
            } }),
            el('button', { type: 'button', class: 'primary', text: 'Save', onclick: function () { saveScratch(scratch.value); } })),
          scratchNote),
        el('div', { class: 'setting' },
          el('h3', { text: 'Holding folder' }),
          el('p', { class: 'muted small', text: 'When on, replaced originals wait in a holding folder until you clear it, so you can be sure the new files are good.' }),
          el('label', { class: 'check', for: 'holding' }, holdBox, el('span', { text: 'Hold originals after replacing them' })),
          el('p', { class: 'small', text: 'Held right now: ' + fmtSize(data.held_bytes) }),
          clearBtn, holdNote),
        el('div', { class: 'setting' },
          el('h3', { text: 'Default language' }),
          el('p', { class: 'muted small', text: 'The soundtrack language to keep in libraries that remove the others. Libraries only remove soundtracks if you turn that on in their options.' }),
          el('label', { class: 'field', for: 'default-language', text: 'Language' }),
          language, languageNote),
        schedule);

      fill(trouble,
        el('p', { class: 'small', text: 'Only needed if you want an AI assistant to look at your history. It can read but never change anything.' }),
        copyBlock('Access token', data.mcp_token),
        copyBlock('Command to connect Claude', data.mcp_command));
    }

    function load() {
      return api('GET', '/api/settings').then(function (loaded) { data = loaded; draw(); }, function (problem) {
        if (problem.status !== 0) fill(box, el('p', { class: 'err', text: messageOf(problem) }));
      });
    }

    // The page reloads itself when the server comes back, so a stop doesn't strand it.
    function retryUntilLoaded() {
      if (data) return;
      load().then(function () { if (!data) window.setTimeout(retryUntilLoaded, REFRESH_MS); });
    }
    retryUntilLoaded();
  }

  // ---- Sign in and first-time password

  function initLogin() {
    var form = byId('login-form');
    var password = byId('password');
    var again = byId('password2');
    var againRow = byId('again-row');
    var error = byId('login-error');
    var go = byId('login-go');
    var firstTime = false;

    function problem(text) {
      error.hidden = !text;
      error.textContent = text || '';
    }

    function learn() {
      api('GET', '/api/auth/state', undefined, { stay: true }).then(function (state) {
        if (state.signed_in) { window.location.href = '/'; return; }
        firstTime = !state.has_password;
        byId('login-title').textContent = firstTime ? 'Welcome to ShelfSaver' : 'Sign in';
        byId('login-help').textContent = firstTime
          ? 'Choose a password to keep your libraries private. Use at least 8 characters.' : '';
        againRow.hidden = !firstTime;
        password.setAttribute('autocomplete', firstTime ? 'new-password' : 'current-password');
        go.textContent = firstTime ? 'Set password' : 'Sign in';
        password.focus();
      }, function () { window.setTimeout(learn, REFRESH_MS); });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      problem('');
      if (firstTime && password.value !== again.value) {
        problem("The two passwords don't match.");
        return;
      }
      go.disabled = true;
      api('POST', firstTime ? '/api/auth/setup' : '/api/auth/login', { password: password.value }, { stay: true })
        .then(function () { window.location.href = '/'; }, function (failure) {
          problem(messageOf(failure));
          go.disabled = false;
        });
    });
    learn();
  }

  if (page === 'home') initHome();
  else if (page === 'library') initLibrary();
  else if (page === 'queue') initQueue();
  else if (page === 'settings') initSettings();
  else if (page === 'login') initLogin();
})();

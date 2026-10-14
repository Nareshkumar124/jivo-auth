/*
 * The board carousel: a timer, a strip, and one of the factory's boards.
 *
 * A port of the factory frontend's carousel hooks (useBoardRotation,
 * useIdleChrome, useOverscan, useFullscreen) onto a plain page. The page
 * never reloads: each board's fragment is fetched as it comes round and
 * again every `refreshSeconds` while it is up, and the last good HTML of
 * every board is kept so a board coming back round paints at once and
 * refreshes behind itself.
 *
 * WALL CLOCK, NOT TICK COUNT. Elapsed time is Date.now() minus the moment the
 * board came up, never an accumulator advanced by the interval: browsers
 * throttle timers in a background tab, and a screen on a wall spends most of
 * its life in exactly that state.
 *
 * NO TIMER WHILE PAUSED. Pausing clears the interval rather than skipping
 * the body, so a held board costs nothing and cannot pile up advances.
 */
(function () {
  'use strict';

  var configEl = document.getElementById('bcx-config');
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);

  var TICK_MS = 250;
  var CHROME_IDLE_MS = 4000;
  var DWELL_KEY = 'board-carousel-dwell-seconds';
  var PAUSED_KEY = 'board-carousel-paused';
  var OVERSCAN_KEY = 'board-carousel-overscan';

  var slides = config.slides || [];
  var count = slides.length;
  var single = !!config.single || count < 2;

  var shell = document.getElementById('bcx');
  var stage = document.getElementById('bcx-stage');
  var strip = document.getElementById('bcx-strip');
  var progressEl = document.getElementById('bcx-progress');
  var progressBar = document.getElementById('bcx-progress-bar');
  var stateEl = document.getElementById('bcx-state');
  var stateText = document.getElementById('bcx-state-text');
  var countMain = document.getElementById('bcx-count-main');
  var countSub = document.getElementById('bcx-count-sub');
  var dots = Array.prototype.slice.call(document.querySelectorAll('.bcx-dot'));
  var prevBtn = document.getElementById('bcx-prev');
  var nextBtn = document.getElementById('bcx-next');
  var pauseBtn = document.getElementById('bcx-pause');
  var dwellSelect = document.getElementById('bcx-dwell-select');
  var fitSelect = document.getElementById('bcx-fit-select');
  var openLink = document.getElementById('bcx-open');
  var fullscreenBtn = document.getElementById('bcx-fullscreen');

  if (!shell || !stage || count === 0) return;

  // ------------------------------------------------------------ storage

  function readNumber(key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      var parsed = raw === null ? NaN : Number(raw);
      return isFinite(parsed) && parsed > 0 ? parsed : fallback;
    } catch (e) {
      // Private windows and locked-down kiosk browsers throw on the accessor
      // itself. A wall screen must still rotate.
      return fallback;
    }
  }

  function readFlag(key) {
    try {
      return window.localStorage.getItem(key) === 'true';
    } catch (e) {
      return false;
    }
  }

  function readOverscan() {
    try {
      var raw = window.localStorage.getItem(OVERSCAN_KEY);
      var parsed = raw === null ? NaN : Number(raw);
      // Only a value actually on the menu. A hand-edited 40 would shrink the
      // board to a postage stamp on a screen nobody is standing at to undo it.
      return config.overscanChoices.indexOf(parsed) >= 0 ? parsed : 0;
    } catch (e) {
      return 0;
    }
  }

  function write(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (e) {
      /* a screen that cannot remember the setting still honours it until reload */
    }
  }

  // ------------------------------------------------------------ state

  var index = 0;
  var paused = single ? false : readFlag(PAUSED_KEY);
  var dwell = readNumber(DWELL_KEY, config.defaultDwellSeconds || 45);
  if (config.dwellChoices.indexOf(dwell) < 0) dwell = config.defaultDwellSeconds || 45;
  var progress = 0;
  var startedAt = 0;
  var timer = null;
  var idleTimer = null;
  var refreshTimer = null;
  var generation = 0;
  var html = {}; // slide key -> last good fragment

  // ------------------------------------------------------------ strip

  function renderProgress() {
    if (progressBar) progressBar.style.width = Math.round(progress * 100) + '%';
    var remaining = Math.max(0, Math.ceil(dwell * (1 - progress)));
    if (countMain) countMain.textContent = paused ? 'held' : remaining + 's';
    if (countSub) countSub.textContent = paused ? 'not rotating' : 'of ' + dwell + 's';
  }

  function renderStrip() {
    dots.forEach(function (dot, position) {
      var active = position === index;
      dot.setAttribute('data-active', active ? 'yes' : 'no');
      dot.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    var pausedFlag = paused ? 'yes' : 'no';
    if (progressEl) progressEl.setAttribute('data-paused', pausedFlag);
    if (stateEl) stateEl.setAttribute('data-paused', pausedFlag);
    if (stateText) stateText.textContent = paused ? 'HELD' : 'ROTATING';

    if (pauseBtn) {
      pauseBtn.setAttribute('aria-pressed', paused ? 'true' : 'false');
      // The label says what pressing it does next, not what the state is.
      var label = paused ? 'Resume rotation (space)' : 'Hold this board (space)';
      pauseBtn.setAttribute('aria-label', paused ? 'Resume rotation' : 'Hold this board');
      pauseBtn.setAttribute('title', label);
      Array.prototype.forEach.call(pauseBtn.querySelectorAll('svg'), function (svg) {
        var show = svg.getAttribute('data-when') === (paused ? 'paused' : 'running');
        if (show) svg.removeAttribute('hidden');
        else svg.setAttribute('hidden', '');
      });
    }

    if (openLink && !single) {
      var current = slides[index];
      openLink.setAttribute('href', current.pageUrl);
      openLink.setAttribute('title', 'Open ' + current.label + ' on its own');
      openLink.setAttribute('aria-label', 'Open ' + current.label + ' on its own');
    }

    if (dwellSelect) dwellSelect.value = String(dwell);
    renderProgress();
  }

  // ------------------------------------------------------------ the stage

  function escape(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function loadingHtml(label) {
    return '<div class="bcx-loading"><span>Loading ' + escape(label) + '…</span></div>';
  }

  function errorHtml(label, detail) {
    return (
      '<div class="bcx-error"><b>' + escape(label) + ' could not be read</b>' +
      '<p>' + escape(detail || 'The board did not answer. It is tried again on the next refresh.') + '</p></div>'
    );
  }

  function mounted(slide) {
    startClocks();
    try {
      document.dispatchEvent(new CustomEvent('boards:mounted', { detail: { key: slide.key, stage: stage } }));
    } catch (e) {
      /* a board's own script is optional */
    }
  }

  function setStale(stale) {
    shell.setAttribute('data-stale', stale ? 'yes' : 'no');
  }

  function fetchSlide(slide, gen) {
    var request = new XMLHttpRequest();
    request.open('GET', slide.fragmentUrl, true);
    request.setRequestHeader('Accept', 'text/html');
    request.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    request.withCredentials = true;
    request.onreadystatechange = function () {
      if (request.readyState !== 4) return;
      if (request.status === 401 || request.status === 403) {
        // The session ended under the screen. The page's own load redirects
        // to the login, which is the right place to end up.
        window.location.reload();
        return;
      }
      if (request.status >= 200 && request.status < 300) {
        html[slide.key] = request.responseText;
        if (gen !== generation) return; // another board came up meanwhile
        stage.innerHTML = request.responseText;
        setStale(false);
        mounted(slide);
        return;
      }
      if (gen !== generation) return;
      if (html[slide.key]) {
        setStale(true);
      } else {
        stage.innerHTML = errorHtml(slide.label, request.status ? 'The server answered ' + request.status + '.' : '');
      }
    };
    request.send();
  }

  function scheduleRefresh(slide, gen) {
    window.clearTimeout(refreshTimer);
    var seconds = Number(slide.refreshSeconds) > 0 ? Number(slide.refreshSeconds) : 60;
    refreshTimer = window.setTimeout(function () {
      if (gen !== generation) return;
      fetchSlide(slide, gen);
      scheduleRefresh(slide, gen);
    }, seconds * 1000);
  }

  function load(position) {
    var slide = slides[position];
    generation += 1;
    var gen = generation;
    var cached = html[slide.key];
    stage.innerHTML = cached || loadingHtml(slide.label);
    stage.setAttribute('data-key', slide.key);
    setStale(false);
    if (cached) mounted(slide);
    fetchSlide(slide, gen);
    scheduleRefresh(slide, gen);
  }

  // ------------------------------------------------------------ rotation

  function stopTimer() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function startTimer() {
    stopTimer();
    if (paused || single) return;
    // This board's turn starts now, and this is the one owner of the clock.
    startedAt = Date.now();
    timer = window.setInterval(function () {
      var elapsed = (Date.now() - startedAt) / 1000;
      if (elapsed >= dwell) {
        show((index + 1) % count);
        return;
      }
      progress = elapsed / dwell;
      renderProgress();
    }, TICK_MS);
  }

  function show(position) {
    if (position < 0 || position >= count) return;
    index = position;
    progress = 0;
    renderStrip();
    load(position);
    startTimer();
  }

  function next() {
    show((index + 1) % count);
  }

  function previous() {
    show((index - 1 + count) % count);
  }

  function goTo(position) {
    // A dot clicked on the board already up is ignored rather than treated as
    // a jump: "restarting" would zero the bar for no reason.
    if (position !== index) show(position);
  }

  function togglePaused() {
    if (single) return;
    paused = !paused;
    write(PAUSED_KEY, paused);
    if (paused) {
      stopTimer();
    } else {
      // Resuming restarts the turn rather than resuming a part-spent one:
      // whoever just un-paused wants to read this board.
      progress = 0;
      startTimer();
    }
    renderStrip();
    wake();
  }

  function setDwell(seconds) {
    dwell = seconds;
    write(DWELL_KEY, seconds);
    progress = 0;
    renderStrip();
    startTimer();
  }

  // ------------------------------------------------------------ idle chrome

  function hideChrome() {
    if (!strip) return;
    strip.setAttribute('data-visible', 'no');
  }

  function wake() {
    if (!strip) return;
    strip.setAttribute('data-visible', 'yes');
    window.clearTimeout(idleTimer);
    // No timer at all while the rotation is held: held means a person stopped
    // it to read one board, and the controls are how they start it again.
    if (paused) return;
    idleTimer = window.setTimeout(hideChrome, CHROME_IDLE_MS);
  }

  ['pointermove', 'pointerdown', 'keydown'].forEach(function (name) {
    window.addEventListener(name, wake);
  });

  // ------------------------------------------------------------ overscan

  function applyOverscan(percent) {
    // The scale takes the inset off BOTH sides: losing 3% of each edge leaves
    // 94% of the width to live in, not 97%.
    var scale = (100 - percent * 2) / 100;
    shell.style.setProperty('--bcx-fit', String(scale));
    shell.style.setProperty('--bcx-pad', percent + '%');
    // `off` skips the transform entirely rather than applying a scale of 1:
    // any transform makes the shell the containing block for the boards'
    // fixed-position panels.
    shell.setAttribute('data-fit', percent > 0 ? 'on' : 'off');
    if (fitSelect) fitSelect.value = String(percent);
  }

  // ------------------------------------------------------------ fullscreen

  function syncFullscreen() {
    var on = document.fullscreenElement === shell;
    shell.setAttribute('data-fullscreen', on ? 'yes' : 'no');
    if (!fullscreenBtn) return;
    fullscreenBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    fullscreenBtn.setAttribute('aria-label', on ? 'Exit fullscreen' : 'Show fullscreen');
    fullscreenBtn.setAttribute('title', on ? 'Exit fullscreen (Esc)' : 'Show fullscreen (F)');
    Array.prototype.forEach.call(fullscreenBtn.querySelectorAll('svg'), function (svg) {
      var showIcon = svg.getAttribute('data-when') === (on ? 'fullscreen' : 'windowed');
      if (showIcon) svg.removeAttribute('hidden');
      else svg.setAttribute('hidden', '');
    });
  }

  function toggleFullscreen() {
    // Only granted from a user gesture; a refusal rejects the promise, which
    // is swallowed rather than surfacing on a screen nobody is standing at.
    if (document.fullscreenElement) {
      var exit = document.exitFullscreen && document.exitFullscreen();
      if (exit && exit.catch) exit.catch(function () {});
      return;
    }
    if (shell.requestFullscreen) {
      var enter = shell.requestFullscreen();
      if (enter && enter.catch) enter.catch(function () {});
    }
  }

  document.addEventListener('fullscreenchange', syncFullscreen);

  // ------------------------------------------------------------ clocks

  // A board's topbar clock is stamped when the fragment is composed; this
  // keeps it moving between refreshes. Any element carrying data-clock.
  var clockTimer = null;

  function pad(n) {
    return (n < 10 ? '0' : '') + n;
  }

  function tickClocks() {
    var now = new Date();
    var text = pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
    Array.prototype.forEach.call(stage.querySelectorAll('[data-clock]'), function (el) {
      el.textContent = text;
    });
  }

  function startClocks() {
    window.clearInterval(clockTimer);
    if (!stage.querySelector('[data-clock]')) return;
    tickClocks();
    clockTimer = window.setInterval(tickClocks, 1000);
  }

  // ------------------------------------------------------------ controls

  dots.forEach(function (dot) {
    dot.addEventListener('click', function () {
      goTo(Number(dot.getAttribute('data-index')));
    });
  });
  if (prevBtn) prevBtn.addEventListener('click', previous);
  if (nextBtn) nextBtn.addEventListener('click', next);
  if (pauseBtn) pauseBtn.addEventListener('click', togglePaused);
  if (fullscreenBtn) fullscreenBtn.addEventListener('click', toggleFullscreen);
  if (dwellSelect) {
    dwellSelect.addEventListener('change', function () {
      setDwell(Number(dwellSelect.value));
    });
  }
  if (fitSelect) {
    fitSelect.addEventListener('change', function () {
      var percent = Number(fitSelect.value);
      write(OVERSCAN_KEY, percent);
      applyOverscan(percent);
    });
  }

  // The keys somebody with a keyboard would try. Bound on the window: an
  // unattended screen has nothing focused. Typing in a field is left alone.
  window.addEventListener('keydown', function (event) {
    var target = event.target;
    if (
      target &&
      (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].indexOf(target.tagName) >= 0)
    ) {
      return;
    }
    if (event.key === ' ' || event.key === 'Spacebar') {
      event.preventDefault();
      togglePaused();
    } else if (event.key === 'ArrowRight') {
      next();
    } else if (event.key === 'ArrowLeft') {
      previous();
    } else if (event.key === 'f' || event.key === 'F') {
      toggleFullscreen();
    } else if (/^[1-9]$/.test(event.key)) {
      goTo(Number(event.key) - 1);
    }
  });

  // ------------------------------------------------------------ start

  applyOverscan(readOverscan());
  syncFullscreen();
  show(0);
  wake();
})();

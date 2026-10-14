/*
 * The accounts board's rotating detail panel: the factory's
 * useSectionRotation hook, without React.
 *
 * The breakdown-detail panel is the only one on the board with nothing to
 * show at rest, so it cycles the four breakdown lines (Vendor A/P, then
 * Expenses, Salary adjustment, Payment pending), each with its G/L heads
 * under it. The fragment is inserted with innerHTML, so nothing inline can
 * run: this listens for the carousel's `boards:mounted` event instead.
 *
 * Two mechanics are kept from the hook, both learned on a real wall:
 *
 * - Wall clock, not tick count. Elapsed time is `Date.now()` minus the moment
 *   this line came up, never an accumulator advanced by the interval:
 *   browsers throttle timers in a background tab, and counting ticks would
 *   make it rotate slower the longer nobody looked at it.
 * - No timer while stopped. Stopping clears the interval rather than
 *   skipping the body, so a stopped stage costs nothing and cannot build a
 *   backlog of advances that all fire at once.
 *
 * Reduced motion stops the rotation entirely rather than merely speeding it
 * up, and is watched rather than read once: a wall screen is set up and
 * left, and somebody changing the OS setting should not have to reload the
 * page to be obeyed. The breakdown rail beside the panel still lists every
 * line's amount, so nothing becomes unreachable.
 */
(function () {
  'use strict';

  /** How long each line holds the stage. */
  var DWELL_MS = 12000;
  /** How often the progress bar is refreshed. Not how often the stage changes. */
  var TICK_MS = 100;

  var timer = null;
  var startedAt = 0;
  var index = 0;
  var stages = [];
  var counter = null;
  var fill = null;
  var mountedStage = null;

  var query = null;
  try {
    query = window.matchMedia('(prefers-reduced-motion: reduce)');
  } catch (e) {
    // No matchMedia: motion is the documented default.
    query = null;
  }

  function reducedMotion() {
    return !!(query && query.matches);
  }

  function clearTimer() {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  /** Forget the board entirely: another slide is up, or this one is gone. */
  function stop() {
    clearTimer();
    stages = [];
    counter = null;
    fill = null;
    mountedStage = null;
  }

  function show(which) {
    stages.forEach(function (el, n) {
      el.hidden = n !== which;
    });
    if (counter) counter.textContent = which + 1 + ' / ' + stages.length;
    if (fill) fill.style.width = '0%';
  }

  function tick() {
    var elapsed = Date.now() - startedAt;
    if (elapsed >= DWELL_MS) {
      startedAt = Date.now();
      index = (index + 1) % stages.length;
      show(index);
      return;
    }
    if (fill) fill.style.width = (elapsed / DWELL_MS) * 100 + '%';
  }

  /** Start (or restart) the clock on the lines currently in the stage. */
  function run() {
    clearTimer();
    // Fewer than two lines, or a viewer who asked for less motion: the
    // first line stays up and the bar does not creep.
    if (stages.length < 2 || reducedMotion()) {
      if (fill) fill.style.width = '0%';
      return;
    }
    // This line's turn starts now. One owner of the clock: every way of
    // changing line restarts it here.
    startedAt = Date.now();
    timer = window.setInterval(tick, TICK_MS);
  }

  function start(stage) {
    stop();
    if (!stage || !stage.querySelector) return;
    mountedStage = stage;
    stages = Array.prototype.slice.call(stage.querySelectorAll('[data-ab-stage]'));
    counter = stage.querySelector('[data-ab-counter]');
    fill = stage.querySelector('[data-ab-progress]');
    if (stages.length === 0) return;
    // A refresh replaces the fragment under a running rotation. Keep the
    // line that was up rather than snapping back to the first every five
    // minutes; a list that shrank under a stale index must not blank the
    // stage.
    index = Math.min(index, stages.length - 1);
    show(index);
    run();
  }

  if (query) {
    var onChange = function () {
      if (mountedStage) run();
    };
    try {
      query.addEventListener('change', onChange);
    } catch (e) {
      if (query.addListener) query.addListener(onChange);
    }
  }

  document.addEventListener('boards:mounted', function (event) {
    var detail = event && event.detail;
    if (!detail || detail.key !== 'accounts') {
      stop();
      return;
    }
    start(detail.stage);
  });
})();

/*
 * built board: behaviour on boards:mounted.
 *
 * The fragment is inserted with innerHTML, so nothing inline runs; the
 * carousel page dispatches `boards:mounted` instead, with the slide's key
 * and the stage element. A built board's key is `built:<slug>`.
 *
 * THE ONE THING THIS DOES
 * The carousel re-fetches a board every refresh and swaps the HTML in whole,
 * which replays every entrance animation: each meter, column and pair bar
 * grows in from zero again. On the first paint of a board that is the wall
 * boards' own look and it stays. On a REFRESH of the board already on screen
 * it reads as a screen that keeps reloading, so the view is marked settled
 * and built.css switches the animations off. The mark is per key: another
 * board coming round (or this one coming back after a rotation) paints with
 * its entrance again.
 *
 * No timers: there is nothing here that would need clearing when another
 * key mounts. The clock is ticked by the carousel page itself (data-clock).
 */
(function () {
  'use strict';

  var PREFIX = 'built:';
  var shown = null; // the built board currently on the stage, by key

  document.addEventListener('boards:mounted', function (event) {
    var detail = event.detail || {};
    var key = String(detail.key || '');

    if (key.indexOf(PREFIX) !== 0) {
      // A fixed board took the stage; the next built board starts fresh.
      shown = null;
      return;
    }

    var stage = detail.stage;
    var view = stage && stage.querySelector ? stage.querySelector('.cbx-view') : null;
    if (!view) {
      shown = key;
      return;
    }

    if (shown === key) {
      view.setAttribute('data-settled', 'yes');
    }
    shown = key;
  });
})();

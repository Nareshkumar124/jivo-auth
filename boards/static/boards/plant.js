/*
 * The plant board needs no script of its own.
 *
 * Everything on it is read, never touched: the factory's drill-down panels
 * (the one interactive thing its page had) are not ported, and the topbar
 * clock is kept ticking by the carousel for any element carrying
 * `data-clock`. Kept because the slide lists it; a behaviour added later
 * listens for the carousel's `boards:mounted` event (`event.detail.key ===
 * 'plant'`), as accounts.js does.
 */

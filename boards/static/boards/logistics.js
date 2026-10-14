/*
 * The logistics board needs no script of its own.
 *
 * The factory page's only moving part on a wall is its clock, and the
 * carousel keeps every [data-clock] ticking already. Its other behaviour --
 * the drill-down panels behind each tile and the settings cog -- belongs to
 * a desk, not a wall, and is not ported (see boards/services/logistics.py).
 * Kept, and listed on the slide, so a later behaviour has somewhere to live:
 * it would listen for `boards:mounted` with `event.detail.key === 'logistics'`
 * the way accounts.js does.
 */

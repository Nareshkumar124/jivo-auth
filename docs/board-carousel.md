# The JI dashboards

The JI Factory app's control boards are part of the ticket system at
**https://auth.jivo.in/**, in its **Dashboard** section
(**https://auth.jivo.in/dashboards/**): the Admin, Plant, Logistics Control
and Accounts boards, followed by any board composed in the factory's
dashboard builder and flagged onto its rotation. The **wall carousel**
(`/dashboards/carousel/`) rotates them all on a wall screen.

The boards are composed by Jivo Auth itself, straight from the factory
application's databases (its PostgreSQL database, read-only, and SAP HANA
for the tiles that read SAP). Nothing calls the factory application's API,
so the dashboards keep working while that application is being deployed.

## Who can see them

Anyone holding the permission **Can see the JI dashboards**
(`boards.view_dashboards`), and every superuser. Everyone else gets a 403;
someone who isn't signed in is sent to the sign-in page at `/login/`.
Being staff gives nothing here.

Give the permission in either place; both change the same permission:

- **In the ticket system:** Administration → **People and roles** →
  *Give dashboard access* (by email), or *Give dashboards* / *Remove
  dashboards* on a person's row. The *JI dashboards* column says how each
  person sees them (given here, through a staff role, or as a superuser),
  and the role filter lists everyone who can. Ticket administrators do this.
- **In the auth admin:** /admin/ → Users → the person → **User
  permissions** → *JI dashboards | JI dashboard | Can see the JI
  dashboards*, or the **Dashboard viewer** staff role (created
  automatically with the other default roles). Superusers do this.

People who can see the dashboards get the **Dashboard** section in the
sidebar: *All dashboards*, one entry per board, and *Wall carousel*.

## Using the boards

- **In the ticket system:** each board opens at `/dashboards/<board>/`
  inside the usual layout, drawn in a 16:9 frame (the factory's wall
  format). **Full screen** fills the screen with just the board; **New tab**
  opens the board on its own. Each board refreshes in place every 60
  seconds.
- **On a wall screen:** create an account for the screen, give it dashboard
  access (it needs nothing else), sign in at `/login/` and open
  `/dashboards/carousel/`. Press **F** (or the fullscreen button) to fill
  the screen. The controls fade after a few seconds without a pointer and
  come back on any movement or key. The strip shows which board is up, how
  long it has left, and one dot per board. **Space** holds and releases the
  rotation, **←/→** step through the boards, **1–9** jump to a board, the
  home button returns to the dashboards. The dwell (how long each board
  holds the screen) and the *Fit* setting (for a television that crops the
  picture it is sent) are remembered per browser.
- A board that could not be refreshed keeps its last good figures and
  marks its live pill amber until the next refresh succeeds.

Each board is also available full window at `/dashboards/<board>/screen/`
(`admin`, `plant`, `logistics`, `accounts`, or `built/<slug>`), and as JSON
at `/dashboards/<board>/data/`.

## What a tile shows when it can't be read

A tile that could not be read shows **not read**, never a zero, and the
reason appears in the chip strip at the bottom right of the board: the SAP
connection isn't configured, the factory database didn't answer, a query
was refused. A warning chip reports caveats on figures that did render.

Figures that depend on SAP (output, storage, dispatch, the production plan)
need `FACTORY_HANA_*` to be set; without it those tiles say so and the rest
of each board shows. The Accounts board reads no SAP at all.

On the Accounts board the names of people holding cash or advances are
always masked: a screen in a corridor must not name them.

## Configuration

All settings come from `.env` (see `.env.example`):

| Variable | Meaning |
|---|---|
| `FACTORY_DB_NAME`, `FACTORY_DB_USER`, `FACTORY_DB_PASSWORD`, `FACTORY_DB_HOST`, `FACTORY_DB_PORT` | The factory application's PostgreSQL database. Read-only: every connection is opened with `default_transaction_read_only=on`. With the database on the same host as the container (host networking), `FACTORY_DB_HOST=127.0.0.1`. Leave `FACTORY_DB_NAME` empty to run without the boards' data (the carousel still opens; every tile says the database isn't configured). |
| `FACTORY_DB_CONNECT_TIMEOUT`, `FACTORY_DB_STATEMENT_TIMEOUT` | Seconds. A board build opens one connection; a statement past the timeout costs that tile only. |
| `FACTORY_HANA_HOST`, `FACTORY_HANA_PORT`, `FACTORY_HANA_USER`, `FACTORY_HANA_PASSWORD` | SAP HANA, for the SAP tiles. Optional. |
| `FACTORY_HANA_SCHEMAS` | Company code to HANA schema, e.g. `JIVO_OIL=<schema>,JIVO_MART=<schema>`. The Admin board adds Oil's and Mart's dispatch together, so both are needed for that tile. |
| `FACTORY_COMPANY_CODE` | The factory company whose boards are shown. Default `JIVO_OIL`. |
| `BOARDS_TIME_ZONE` | The factory's local time zone for "today" and "this month". Default `Asia/Kolkata`. |
| `BOARDS_CACHE_SECONDS` | A composed board is reused this long, so several screens showing the same board don't each query SAP every refresh. Default 20; 0 disables. |

The PostgreSQL role used here only needs `CONNECT` on the database and
`SELECT` on the tables the boards read. Prefer a read-only role over the
factory application's own role.

## Where the figures come from

| Board | Reads |
|---|---|
| Admin Control | SAP (production receipts, finished-goods and packaging stock, invoices for Oil and Mart, the production plan) and the factory database (labour gate, electricity readings, salaries, maintenance) for the cost donut. The tank farm's own system (EXIM) is not read, so the oil tile reports no rated capacity, as the factory's board does without it. |
| Plant Control | SAP (open purchase orders, goods receipts, stock in the packaging stores, production receipts, the plan) and the factory database (blowing runs, production runs, waste logs, PF stock movements, BST box scans, PO receipts, the board's own settings). |
| Logistics Control | The feeds the factory's page composes in the browser: warehouse occupancy, non-moving stock, stock in transit, dispatch plans and fulfilment, freight, GRPO queue, PF movements, the employee roll and the factory expense board. |
| Accounts | The cash book only: receipts, payments, approvals, bunches sent to head office, advances and ATM accounts. |
| Built boards | The factory's dashboard builder: each published wall board flagged onto the rotation, with its cards computed from the same catalogue (section label, dispatch stage clock, gate truck turnaround, trucks on site, production line downtime). Private boards are never shown. |

The exact definitions (what counts as produced, how an average per active
day is taken, which warehouses a tile covers, which customers are
intercompany) are the factory's and are documented in each service under
`boards/services/`.

## What is not ported

- Drill-down panels (clicking a tile for its detail) and the boards'
  settings pages. The settings are read from the factory's database as the
  factory saved them.
- The EXIM tank farm read on the Admin board.
- Per-board permissions: the dashboards permission shows every board and every tile.

## Troubleshooting

| Symptom | Check |
|---|---|
| Every tile says the factory database is not configured | `FACTORY_DB_NAME`, `FACTORY_DB_USER` and `FACTORY_DB_HOST` in `.env`, then restart the container. |
| Every tile says the database could not be reached | The host and port from inside the container, `pg_hba.conf` for the role, the role's password. The reason chip carries the driver's message. |
| SAP tiles say SAP is not configured | `FACTORY_HANA_HOST`, `FACTORY_HANA_USER`, `FACTORY_HANA_PASSWORD` and the company's entry in `FACTORY_HANA_SCHEMAS`. |
| A tile says a query was refused | The factory application's schema changed under the board. The reason chip names the column; the query is in `boards/services/<board>.py`. |
| The screen shows the sign-in page | The session expired. The display account signs in again at `/login/`; sessions last as long as `SESSION_COOKIE_AGE` (two weeks by default). |

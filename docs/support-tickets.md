# Support tickets

**https://auth.jivo.in/** is the support desk for every Jivo application.
People sign in there with their Jivo account and land on a dashboard that
fits their role: they raise tickets and follow them, the team responsible
for the application handles them, and ticket administrators run the whole
desk.

The ticket system is its own application inside this service, with its own
pages, roles and permissions. It doesn't use the Django admin: **/admin/**
stays the auth system's admin panel (users, applications' sign-in access,
sessions, audit log), and has no ticket pages.

## How tickets are routed

```text
Department ──► Application ──► Responsible team ──► Ticket
IT Department   OMS             OMS Team
                JI Factory App  JI Factory Team
                JSAP            JSAP Team
```

- A **department** groups the applications people choose from on the
  ticket form. It can name a *default team* for applications that have no
  team of their own.
- An **application** is the same record other Jivo apps use for sign-in.
  The ticket system adds three things to it, edited under
  Administration → Applications: its **department**, its **responsible
  team**, and whether it **accepts tickets**. Changing the team only
  affects *new* tickets; existing ones keep theirs until moved. Applications
  that don't use Jivo sign-in can be added there too (no API key, no users).
- A **team** has members, each a *member* or a *team manager*. Members get
  the team queue and an email when a ticket arrives.

Nothing is hardcoded: departments, applications, teams, members and roles
are all managed in the ticket system.

## Roles

| Role | Who | Can |
|---|---|---|
| **User** | Any active, verified Jivo account | Raise tickets, follow their own, reply, attach files, close a resolved ticket, reopen it within the reopen window. |
| **Team member** | In an active team (Administration → Teams and members) | For the team's tickets: claim, reply, add internal notes, request information, change status, resolve, close, reopen; assign and change priority if Settings allow it. |
| **Team manager** | A member with the manager role | Everything a member can, plus always assign and change priority, move a member's tickets to someone else, and the team's statistics. |
| **Administrator** | Given under Administration → People and roles, or any superuser | Everything, on every ticket, plus all of Administration. |

The roles are stored by the ticket system itself: being staff, or holding a
staff role in /admin/, gives nothing here. Every check runs on the server, so
a typed URL or an API call gets the same answer as a button: other people's
tickets answer 403 (pages) or 404 (API), /manage/ answers 403 to anyone but
an administrator, and internal notes and their attachments are only served
to the responsible team and administrators.

## Pages

| Page | Path | For |
|---|---|---|
| Sign in, then the dashboard | `/` | Everyone. Sections by role: my tickets; my teams' queue (new, available, assigned to me, in progress, waiting, resolved, overdue); teams I manage (performance and who holds what); the whole desk for administrators. |
| Raise ticket | `/tickets/new/` | Department, application, subject, description, attachments. |
| My tickets | `/tickets/` | Search, status, priority, application, sorting, pages. |
| A ticket | `/tickets/<id>/` | One page for everyone, with the controls the person's role allows: description, attachments, conversation, internal notes, resolution, details and deadlines, actions, history. |
| Team queues | `/team/available/`, `/team/mine/`, `/team/all/`, `/team/resolved/` | Team members. |
| Team overview | `/team/`, `/team/<id>/` | Team members; managers also get the reassign tool. |
| Notifications | `/notifications/` | Everyone. |
| Administration | `/manage/…` | Administrators: all tickets, departments, applications, teams and members, people and roles, reports, activity history, settings. |

## Workflow

```text
          claim / assign                 request information
 Open ───────────────► In progress ◄───────────────────────► Waiting for information
   ▲                       │                                   │ (the reply of the person who
   │ reopen                │ resolve (with a summary)          │  raised it moves it back)
   │                       ▼
 Reopened ◄──────────── Resolved ──────► Closed
   (counts as open)      (closed by them, the team or an administrator,
                          or automatically after N days)
```

- **Open**: raised, waiting for a team member. Unassigned tickets are
  *available to claim*. Claiming locks the ticket row, so two people can't
  take the same ticket.
- **In progress**: someone owns it.
- **Waiting for information**: the team asked something. The reply of the
  person who raised it puts the ticket back in progress (or open, if nobody
  owns it).
- **Resolved**: a resolution summary is recorded and the person is told.
- **Closed**: complete. Teams and administrators close from any active
  state (duplicates, for instance); the person who raised it closes it once
  resolved. `manage.py close_resolved_tickets` closes resolved tickets
  nobody answered within *Close resolved tickets after (days)*; run it daily.
- **Reopened**: back from resolved or closed, by the team, an administrator,
  or the person who raised it within the reopen window. Deadlines restart.

Every change (raising, assignment, status, priority, replies, notes,
information requests, resolution, closing, reopening, attachments, team
moves) is a **ticket event** with who did it and when, shown on the ticket
and under Administration → Activity history.

## Deadlines and priorities

Priorities are *low*, *medium* (default), *high* and *urgent*. Settings hold,
per priority, the hours allowed for the first response and for the
resolution. Deadlines are set when a ticket is raised or reopened and
recomputed when its priority changes. A ticket past either one is
**overdue**: red on every list, counted on the dashboards, and filterable
(*SLA* in All tickets).

## Notifications

Each person gets an in-app notification (the bell) and an email linking to
the ticket:

| Event | Who is told |
|---|---|
| New ticket | Every active member of the responsible team (can be turned off in Settings) |
| Assigned, reassigned | The new assignee; the previous one on reassignment |
| Status or priority change | The person who raised it and the assignee, unless they made the change |
| Request for information | The person who raised it |
| Reply | The person who raised it, for a team reply; the assignee (or the team) for theirs |
| Internal note | The assignee |
| Resolved, closed, reopened | The person who raised it and the assignee (or the team) |

Emails go out through the service's `MAILERS` settings. A mail outage is
logged and never fails the action; the in-app notification is there either
way.

## Attachments

Screenshots, logs and documents can be attached to a ticket or a reply.
Settings decide the largest file (10 MB by default) and the allowed
extensions; executables, scripts and HTML are always refused, and images
must really be images. Files are stored under `MEDIA_ROOT` (`media/` on the
server, see the [deployment guide](deploy-auth-service.md)) with random
names, and only served after the permission check: images inline,
everything else as a download. Nginx's `client_max_body_size` must allow
the configured size.

## Administration

Everything under **Administration** in the sidebar, for administrators:

| Page | What it does |
|---|---|
| **All tickets** | Every ticket: search (ID, subject, description), filters (status, priority, SLA, department, application, team, assignee, raised and updated date ranges), sorting, pages of 25/50/100, **CSV export** of the filtered list (opens in Excel), and bulk actions with a confirmation step: assign, change priority, change status, move to a team, close, reopen. |
| **Departments** | Add, edit, deactivate; their applications, pending and total tickets, default team. |
| **Applications** | Department, responsible team and "accepts tickets" per application, with ticket counts; filters for applications that can't be routed; add applications that only need tickets. |
| **Teams and members** | Create and edit teams; add members by email, make them managers, remove them (their active tickets go back to the queue); move a member's tickets when they're away; the team's applications and workload. |
| **People and roles** | Everyone with their ticket role, teams and workload; make someone an administrator or remove the role. Accounts themselves are created in /admin/. |
| **Reports and analytics** | Date range, department, application and team filters; tickets over time; by department, application, team, status and priority; team members' workload; average first response and resolution; share resolved within SLA; pending and overdue per day; CSV exports. |
| **Activity history** | Every ticket event, searchable by ticket, person and action, with date limits. |
| **Settings** | Deadline hours per priority, what members may do, the reopen window, automatic closing, attachment limits, team emails. |

## For applications: the API

Applications can let people raise tickets without leaving them, with the
person's access token:

| Method and path | Purpose |
|---|---|
| `GET /api/v1/support/catalog/` | Departments and the applications that accept tickets. |
| `GET /api/v1/support/tickets/` | The person's tickets (`?scope=team` for team members and administrators; `?status=active` or a status). |
| `POST /api/v1/support/tickets/` | Raise a ticket (`department`, `application` slug, `subject`, `description`). |
| `GET /api/v1/support/tickets/{id}/` | A ticket with its conversation and attachment details. |
| `POST /api/v1/support/tickets/{id}/comments/` | Reply (`body`; `is_internal` for team notes). |

Attachments are added in the ticket system; the API returns each ticket's
`url` there. The reference is in Swagger at `/api/docs/` under **Support**.
The simplest integration is a link to `https://auth.jivo.in/tickets/new/`.

## Setting it up

1. Make the first administrator: sign in at https://auth.jivo.in/ as a
   superuser, open **People and roles** and add them by email.
2. **Departments** → add one, e.g. *IT Department*.
3. **Teams and members** → add teams (*OMS Team*, *JSAP Team*, ...) and their
   members; mark the managers. Members need no staff access.
4. **Applications** → for each application, set the department and the
   responsible team.
5. **Settings** → check the deadline hours, attachment limits and who may
   assign.
6. On the server: the `media/` directory, Nginx's `client_max_body_size`,
   and a daily `docker compose exec -T auth python manage.py close_resolved_tickets`
   (deployment guide).

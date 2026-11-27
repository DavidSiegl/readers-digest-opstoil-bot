# TODO

## Planned

Features a fuller version could have; none is started.

- **Answered and flagged mail.** Read IMAP flags (`\Answered`, `\Flagged`)
  so mail already answered drops out of "for you".
- **Gone from the inbox.** Notice mail that was moved or deleted in another
  client, and stop listing it.
- **Replying.** An editor for the model's reply draft and an outbox that
  sends it after the reader confirms, instead of the copy button.
- **Ticket-system actions.** Look up or change a ticket in its system (take
  it over, close it) from the digest or the window, per ticket system.
- **Teams notification mail.** Parse Teams' "you have unread messages" mail
  as a fallback when the web session is logged out.
- **A `mail` command.** `show` lists inbox mail since the previous working
  day; nothing prints one mail whole with its review tag and draft, by
  Message-ID, optionally as JSON for scripts. Closed by such a command
  reading only the state db.
- **Teams channels.** Read channel posts that mention the reader, not only
  chats.

## Open issues

- **The window can still wait on a large sync.** Opening the state db
  takes no write lock when it is current, but SQLite's rollback journal
  makes readers wait while a writer whose changes outgrew its page cache
  holds the exclusive lock: a backfill or `sync --rescan` of a big
  folder can freeze the window for up to the 30 s busy timeout. Closed by
  reading in the window off the GTK main loop, or by WAL with its `-wal`
  and `-shm` files made owner-only like the db.
- **A rejected IMAP login is tried again next run.** `SourceError`
  carries `login_rejected`, but nothing reads it: the timer and the
  window's Sync button run the password command and LOGIN on every sync,
  so after a password change each run sends the stale one again, which
  counts towards a lockout. Within a run it is not retried. Closed by
  recording a rejection per account and refusing to log in while the
  password command still prints the password that was rejected (kept as
  a keyed hash, never as the password), or until the user clears it.
- **Teams writes outside the chat service go through.** The route
  matches only `/api/chatsvc/`. Every other write the full Teams client
  makes goes out unblocked during a headless run: presence and endpoint
  registration, csa, activity-feed and read-state calls on other hosts or
  paths. The README says only what is enforced.
  Closed by blocking every non-GET to Teams hosts except what sign-in and
  token refresh need, found with `teams-probe` against a real tenant.
- **A ticket's state must be in its subject.** The rules read only
  Subject and From, and a `tickets` folder's text is never fetched, so a
  tracker's subjects must name id, state and category. Most trackers'
  do not: GitHub, GitLab and Jira
  Cloud put a state change in the body ("Closed #12 as completed"), and
  Bugzilla and Redmine in headers (`X-Bugzilla-Status`,
  `X-Redmine-Issue-Status`). With those, every ticket stays open until
  `window_days` hides it. Closed by rules
  that can also match named headers and the head of the body, with that
  text kept for re-matching (`retention_days` would otherwise stop a rule
  change from re-reading the past).
- **Ticket ids from different trackers collide.** `ticket.id` is the
  bare id a rule read, so helpdesk request 12 and GitHub issue 12 fold
  into one ticket, as do issue 12 of two repos. Only a tracker that
  numbers tickets across the whole system is safe. Closed by keying a
  ticket by the tracker it is in plus its id, and letting `url` use every
  named group (a GitHub link needs the repo as well as the number).
- **Focus means category.** `[digest] focus` filters on the queue or
  group, which says who has to act only in trackers that route tickets
  to queues. In Jira or GitHub the
  project says little; the assignee says who has to act. Closed, if wanted,
  by an `assignee` group that focus can also match.

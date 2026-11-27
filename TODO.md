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
- **A `mail` command.** Review 01 F8: "the reference's `mail` command (inbox
  mail since the previous working day) [has a counterpart] in neither
  `cli.py` nor `TODO.md`." `show` lists that mail already; what is missing
  is printing one mail whole with its review tag and draft, by Message-ID,
  optionally as JSON for scripts. Closed by such a command reading only the
  state db.
- **Teams channels.** Read channel posts that mention the reader, not only
  chats.

## Open issues

- **Teams writes outside the chat service go through.** Review 01 F3:
  "the route matches only `/api/chatsvc/`. Every other write the full
  Teams client makes goes out unblocked during a headless run:
  presence/endpoint registration, csa, activity-feed and read-state calls
  on other hosts or paths." The README now says only what is enforced.
  Closed by blocking every non-GET to Teams hosts except what sign-in and
  token refresh need, found with `teams-probe` against a real tenant.
- **A ticket's state must be in its subject.** The rules read only
  Subject and From, and a `tickets` folder's text is never fetched: the
  shape of the change-ticket subjects this tool was first written for,
  which name id, state and category. Most trackers do not: GitHub, GitLab and Jira
  Cloud put a state change in the body ("Closed #12 as completed"), and
  Bugzilla and Redmine in headers (`X-Bugzilla-Status`,
  `X-Redmine-Issue-Status`). With those, every ticket stays open until
  `window_days` hides it. The example config's Jira rule reads a
  " - status changed to Done" suffix Jira does not send. Closed by rules
  that can also match named headers and the head of the body, with that
  text kept for re-matching (`retention_days` would otherwise stop a rule
  change from re-reading the past).
- **Ticket ids from different trackers collide.** `ticket.id` is the
  bare id a rule read, so helpdesk request 12 and GitHub issue 12 fold
  into one ticket, as do issue 12 of two repos. The tracker this tool was
  first written for numbers tickets across the whole system, so it never
  met this. Closed by keying a
  ticket by the tracker it is in plus its id, and letting `url` use every
  named group (a GitHub link needs the repo as well as the number).
- **Focus means category.** `[digest] focus` filters on the queue or
  group, which in the tracker this tool was first written for is who has
  to act. In Jira or GitHub the
  project says little; the assignee says who has to act. Closed, if wanted,
  by an `assignee` group that focus can also match.

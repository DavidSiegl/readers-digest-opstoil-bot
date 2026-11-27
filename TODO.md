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

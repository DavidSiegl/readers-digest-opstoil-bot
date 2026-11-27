<p align="center">
  <img src="src/readers_digest/gui/icons/readers-digest.svg" width="128" height="128"
       alt="A little bot in reading glasses, reading its digest">
</p>

<!-- The Python badge repeats pyproject.toml's requires-python, and the review badge what
     "Review" says a review command may be: a change to one changes the other. -->
<p align="center">
  <a href="https://github.com/DavidSiegl/readers-digest-opstoil-bot/releases/latest"><img src="https://img.shields.io/github/v/release/DavidSiegl/readers-digest-opstoil-bot" alt="Latest release"></a>
  <a href="https://github.com/DavidSiegl/readers-digest-opstoil-bot/releases"><img src="https://img.shields.io/github/downloads/DavidSiegl/readers-digest-opstoil-bot/total?label=asset%20downloads" alt="Downloads of all release assets"></a>
  <a href="https://github.com/DavidSiegl/readers-digest-opstoil-bot/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/DavidSiegl/readers-digest-opstoil-bot/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="#review"><img src="https://img.shields.io/badge/LLM%20review-any%20model%2C%20local%20or%20hosted-informational" alt="LLM review: any model, local or hosted"></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/built%20with-Python%203.12%2B-3776AB?logo=python&logoColor=white" alt="Built with Python 3.12 or later"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/DavidSiegl/readers-digest-opstoil-bot" alt="License"></a>
  <a href="https://github.com/DavidSiegl/readers-digest-opstoil-bot/commits/main"><img src="https://img.shields.io/github/last-commit/DavidSiegl/readers-digest-opstoil-bot" alt="Last commit"></a>
  <a href="https://ko-fi.com/davidsiegl"><img src="https://img.shields.io/badge/Ko--fi-support%20me-FF5E5B?logo=kofi&logoColor=white" alt="Support me on Ko-fi"></a>
</p>

# A Reader's Digest Opstoil Bot

A daily digest of your open tickets, new mail and unread Teams chats,
triaged by a language model of your choice, and read without changing a
thing in your mailbox.

Ticket systems send a mail for every change: created, assigned, commented,
state changed, reminder. `readers-digest` reads those notifications over
IMAP, works out from their subjects which tickets are open and what state
each is in, and writes one short digest: what is open by state, what is new
since the previous working day, what has gone quiet, and what closed.

The rest of your mail and your unread Teams chats go to a model review.
Connect any language model you can run as a command, local or hosted, and
it reads each new mail and chat and tags it with who has to act (you, your
team or nobody), what it is (request, question, alert, FYI or noise), a
one-line summary, and for whatever asks you for an answer a reply draft.
The digest then puts the mail that needs you first and FYI and noise
last, and `review.md` lists every entry with its summary and draft. The
review is off until you name the command, so nothing leaves your machine
before you choose where it goes (see [Review](#review)).

```markdown
# Tickets in focus, 2026-11-27 12:01

## Open (4)

### Open (3)

- [OPS-1301](<https://jira.example.com/browse/OPS-1301>) · Backup job slow on db02 · OPS · open 9d
- [OPS-1290](<https://jira.example.com/browse/OPS-1290>) · Rotate TLS certificates · OPS · open 6d
- [OPS-1312](<https://jira.example.com/browse/OPS-1312>) · Disk full on db01 · OPS · open 3h

### In Progress (1)

- [48213](<https://helpdesk.example.com/requests/48213>) · Printer jammed · Helpdesk · open 20h · 1 reminder

## New since 2026-11-26 00:00 (2)
...
## Stale: no mail for 5 days or more (2)
...
## Closed since 2026-11-26 00:00 (1)

- [48190](<https://helpdesk.example.com/requests/48190>) · VPN token expired · Helpdesk · Resolved

## New mail in work/INBOX since 2026-11-26 00:00 (2)

- 2026-11-27 08:01 · [me · request] · Sam Example · Can you look at the restore test?
  - Sam asks you to check last night's restore test before the 15:00 change.

FYI / noise (1):

- 2026-11-27 06:30 · [nobody · fyi] · Facilities · Lift maintenance on Friday
  - The lifts are out of service on Friday morning.

---

Last successful sync: 2026-11-27 12:01 · Unparsed subjects: 0
```

And the model review's own page, `review.md`:

```markdown
# Review, 2026-11-27 12:01

2 new entries since 2026-11-26 00:00.

> Written by a language model from text other people wrote. Check it against the source before acting.

## For you (1)

- **mail from Sam Example: Can you look at the restore test?** · request — Sam asks you to check last night's restore test before the 15:00 change.

  Draft:

  > Hi Sam, I'll go through the restore test log this morning and get back to you before 15:00.

## For the team (0)

None.

## Nothing to do (1)

- **mail from Facilities: Lift maintenance on Friday** · fyi — The lifts are out of service on Friday morning.
```

It is a command-line tool with an optional GTK window and tray icon; the
window is called Toil Digest. The core needs nothing but the standard
library of Python 3.12 or later.

## What it never does

- It never changes your mailbox. It opens folders with IMAP `EXAMINE` (not
  `SELECT`) and fetches with `BODY.PEEK`, so no mail is marked read,
  flagged, moved or deleted, and nothing it does shows in another client.
- It never posts to Teams. Every write to Teams' chat service is blocked
  in the tool's browser profile, so it never sends a message, reacts, or
  marks a chat read. The rest of the Teams web app runs as in a browser
  while a sync reads, so colleagues may see you as online meanwhile.
- It never types a password into a web page. A password the IMAP server
  rejected is not tried again in the same run, but the next sync tries
  it again (TODO.md, Open issues).
- It never runs or sends what a model wrote. A reply draft goes to the
  clipboard when you click its button; sending it is up to you.

What it keeps: a SQLite database in `~/.local/state/readers-digest/`
(mode 0600) with the headers of the mail it read, and the text of non-ticket
mail and of chat messages for `retention_days` (default 7).

## Install

```sh
git clone https://github.com/DavidSiegl/readers-digest-opstoil-bot.git && cd readers-digest-opstoil-bot
uv tool install .            # or: pipx install .
```

Or download the wheel attached to a
[release](https://github.com/DavidSiegl/readers-digest-opstoil-bot/releases)
and install that, with `<version>` the release's version:

```sh
uv tool install './readers_digest_opstoil_bot-<version>-py3-none-any.whl'          # or, with Teams:
uv tool install './readers_digest_opstoil_bot-<version>-py3-none-any.whl[teams]'   # then playwright, as below
```

The wheel holds no example config: download
[config.example.toml](config.example.toml) from the repository for
[Configure](#configure).

With Teams:

```sh
uv tool install '.[teams]'
~/.local/share/uv/tools/readers-digest-opstoil-bot/bin/playwright install chromium
```

## Configure

```sh
mkdir -p ~/.config/readers-digest
cp config.example.toml ~/.config/readers-digest/config.toml
```

[config.example.toml](config.example.toml) documents every key. What you
have to write yourself:

- **Accounts and folders.** One `[[account]]` per IMAP login, with a
  `password_command` that prints the password (`pass`, `secret-tool`, a
  keyring helper). Each folder has a role: a `tickets` folder holds only
  notifications (a filter in your mail client puts them there), a `mail`
  folder holds anything.
- **Subject rules.** One `[[rule]]` per kind of notification subject: a
  regular expression with named groups for the ticket's id and, where the
  subject has them, its title, state and category, plus an optional URL
  template for a link. The first rule that matches wins. Try one on a real
  subject before you sync:

  ```sh
  readers-digest match 'Request #48213 (Printer jammed): In progress' --from help@helpdesk.example.com
  ```

  Changing the rules needs no rebuild: the next run reads every stored
  subject again with the new rules.
- **Focus.** `[digest] focus` is a regular expression on the category, so the
  digest shows only your team's queues; `show --all` shows every ticket.

Then:

```sh
readers-digest sync      # the first run reads the last backfill_days (30)
readers-digest show      # prints the digest and writes digest.md
```

## Commands

| Command | What it does |
|---|---|
| `sync` | Read new mail from every account (and Teams, if enabled) into the state db. An account whose login fails does not stop the others. |
| `show` | Print the digest and write `digest.md` next to the state db. `--notify` sends a desktop notification for what is new; `--all` ignores focus. |
| `review` | Send the mail and chats not reviewed yet to `[review] command`, store a tag for each, write `review.md`. |
| `match SUBJECT` | Say which rule reads a subject and what it reads; exit 1 when none does. |
| `prune --days N` | Delete what has been quiet for N days, then shrink the db. `--dry-run` only says what would go. |
| `gui` | Open the window with a tray icon. |
| `teams-login` | Log in to Teams once, in the tool's own browser profile. |
| `teams-probe` | Record the shape of Teams' chat calls, without content, into `teams-probe.json` (see [Teams](#teams)). |

`readers-digest --help` and `readers-digest COMMAND --help` list every option.

## Review

`review` is off until you set `[review] command`. The command gets a prompt
on stdin and prints its answer on stdout, so any model will do: a local one
(`ollama run llama3.1`) or a CLI for a hosted one (`llm -m …`).

**Whatever that command talks to sees the text of your mail and chats.**
Pick one you are allowed to send them to.

The prompt fences the mail as data and asks for JSON in a fixed shape that
tags every entry exactly once; an answer that does not is asked for again
once, then refused. Each entry gets who has to act (`me`, `team`,
`nobody`), what it is (`request`, `question`, `alert`, `fyi`, `noise`), a
summary, and, for mail to you, a reply draft. The digest and the window
group mail by these tags. Entries go to the command in batches; when it
fails part way, `review.md` holds the batches it answered, `review` exits
1, and the rest wait for the next run.

## Teams

Install with the `teams` extra (above), set `[teams] enabled = true` and
`account`, and run `readers-digest teams-login` once: a browser window
opens, and you log in the way Microsoft asks. The session stays in a
Chromium profile next to the state db, used for nothing else. Later syncs
reuse it headless and read your unread chats (not channels). When
Microsoft wants the password again, sync says so, and you run
`teams-login` again.

This reads the Teams web app's own calls, which Microsoft does not document
and may change. `readers-digest teams-probe` records their shape, without
content, into `teams-probe.json` for when they do.

## Running it on a timer

[contrib/systemd](contrib/systemd) has a user service that runs `sync` and
then `show --notify --quiet`, and a timer that starts it every 30 minutes on
weekdays:

```sh
cp contrib/systemd/readers-digest.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now readers-digest.timer
```

A failed sync still runs `show`, which puts a banner in the digest and
notifies once per outage.

## The window

`readers-digest gui` shows the digest in a GTK 4 window, with Sync and
Review buttons, links to the tickets, and a copy button for reply drafts.
Closing it leaves an icon in the tray (a StatusNotifierItem: Waybar, KDE,
and GNOME with the AppIndicator extension show it), with a dot when
something new needs you. After an upgrade, the window restarts itself on
the new code once no sync or review is running.

The window needs PyGObject, GTK 4 and libadwaita from your distribution:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1    # Debian, Ubuntu
sudo dnf install python3-gobject gtk4 libadwaita           # Fedora
```

When the tool's own Python cannot import them, `gui` runs the window on
`/usr/bin/python3`. For a launcher entry and its icon:

```sh
cp contrib/desktop/io.github.DavidSiegl.ToilDigest.desktop ~/.local/share/applications/
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp src/readers_digest/gui/icons/readers-digest.svg ~/.local/share/icons/hicolor/scalable/apps/
# To start hidden in the tray at login:
cp contrib/desktop/readers-digest-autostart.desktop ~/.config/autostart/
```

If the launcher does nothing, its PATH lacks `~/.local/bin`: put the full
path to `readers-digest` in the entry's `Exec=`.

## License

MIT, see [LICENSE](LICENSE).

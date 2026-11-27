# Architecture

readers-digest is a pipeline of plain functions around one SQLite file:

```
IMAP (imap.py) ─┐                       ┌─ digest.py ─ show: stdout, digest.md, notify.py
                ├─ store.py (state.db) ─┼─ review.py ─ review: [review] command, review.md
Teams (teams/) ─┘    rules.py           └─ gui/ ─ window and tray
```

`cli.py` wires each command to these modules; `config.py` reads the TOML
into frozen dataclasses that every module takes as arguments, so nothing
reads the config or the environment on its own.

## Principles

**Deterministic first.** Everything in the digest comes from subjects,
headers and the rules in the config: which ticket a mail is about, its
state, whether it is open, new, stale or closed. A language model is an
optional extra (`review`) that only adds tags next to mail; the digest is
complete without it, and a wrong tag never changes a ticket.

**Sources are read-only.** `imap.py` sends EXAMINE and BODY.PEEK and nothing
that writes; its module docstring lists every command it sends, and the
tests assert that list. `teams/web.py` lets only GET, HEAD and OPTIONS
through to Teams' chat service, so it sends no message, reaction or read
mark there; the client's other calls go out as a browser's would (TODO.md, Open
issues). A rejected IMAP login is not retried within a run (the next run
tries again: TODO.md, Open issues), and a Teams
login that needs a password stops the run instead of typing one.

**All input is untrusted.** Subjects, names, mail bodies and chat messages
are written by other people. `digest.clean` puts each on one line before it
reaches Markdown; the window shows text with markup off; the tray escapes
its tooltip. The review prompt fences entries as data, and the model's
answer must be JSON of a fixed shape that `review.check` validates before
anything is stored. Nothing a model writes is run or sent.

**Mail is the source of truth; tickets are folded from it.** The `message`
table keeps every mail sync read, once, by Message-ID. A ticket's row is
recomputed from that ticket's whole history, in Date: order, whenever one
of its mails arrives. So the order in which mail is read never matters, a
sync that re-reads an overlap is harmless, and two folders delivering the
same mail count it once.

**A rule change re-reads the past.** The store keeps a fingerprint of the
rules. Opened with different rules, it matches every stored subject again
and refolds every ticket, so fixing a rule also fixes the mail it misread,
with no rebuild command to remember.

**Decisions at read time.** What counts as closed, in focus, stale or hidden
is decided by `digest.collect` from the config when the digest is built,
not stored. Changing `[digest]` takes effect on the next `show`.

## Modules

| Module | What it owns |
|---|---|
| `config.py` | The TOML schema: defaults, validation (an unknown key is an error), and the dataclasses. |
| `rules.py` | Matching a subject and sender against the rules, giving a `Match`. |
| `imap.py` | One read-only IMAP session per account: cursors (UIDVALIDITY + last UID), backfill and overlap, bodies only for `mail` folders. |
| `store.py` | The SQLite schema, recording mail and chats, folding tickets, cursors, run status per source, tags, announcements, prune. |
| `text.py` | HTML mail and chat messages as plain text. |
| `teams/commands.py` | The Teams side of `sync`, and `teams-login` and `teams-probe`; imports `teams/web.py` only when one runs. |
| `teams/chat.py` | Pure parsing of Teams' chat JSON into conversations and messages; what is unread. |
| `teams/web.py` | The Playwright side: the tool's own Chromium profile, login, read-only fetching. Imported only when Teams is enabled. |
| `teams/shape.py` | The content-free shape of Teams' replies, for `teams-probe`. |
| `digest.py` | Turning the store into a `Digest` for a moment in time, and rendering it as Markdown. |
| `review.py` | Building the review prompt, running the command, checking its answer, rendering review.md. |
| `notify.py` | Deciding what one desktop notification says, so that each thing is announced once. |
| `lock.py` | One sync or review at a time per state db. |
| `gui/model.py` | The digest as pages, groups and rows: plain data, tested without GTK. |
| `gui/window.py`, `gui/tray.py` | GTK 4 widgets for that model, and the StatusNotifierItem tray icon over D-Bus. Sync and review run as subprocesses. |

## Failure

A source that fails (an IMAP account, Teams) is recorded in the store with
its error and time, and the next `show` puts a banner in the digest and
notifies once per outage. One failing account does not stop the others.
Exit status 1 means a command failed; its last stderr line, prefixed
`readers-digest: `, says why.

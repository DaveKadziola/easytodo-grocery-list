# EasyToDo Grocery List

Self-hosted grocery and to-do lists for a household. Everyone has their own account and boards,
boards can be shared, and a change made on one phone shows up on every other open screen in a
moment, without reloading. It runs on your own server or home computer with docker compose, and
works on a home network without internet access.

This is EasyToDo v2.1, the first feature release after v2.0 of the v2 rewrite of the original
single-list Flask app as a multi-user FastAPI + HTMX app. Coming from v1? See
[docs/migration-from-v1.md](docs/migration-from-v1.md). Updating from v2.0? See
[What's new in v2.1](#whats-new-in-v21) and [Upgrading from v2.0](#upgrading-from-v20).

<p align="center">
  <img src="docs/images/board-phone.png" alt="A grocery board on a phone" width="260">
  &nbsp;
  <img src="docs/images/board-desktop.png" alt="The same board on a desktop browser" width="560">
</p>

## Features

- **Boards, categories and items.** As many boards as you like. Items have an optional
  description and are ticked off with one tap. Drag items between categories and reorder
  categories, with a mouse, by touch or from the keyboard (Move up / Move down in a category's
  menu). Fold a category away on one device. "Clear done" empties a category of ticked items.
  Move a category, with its items, to another board.
- **Real-time sync.** Every open board stays current over a WebSocket. A phone that was asleep or
  lost its signal catches up as soon as it is back. The header shows "Live" (on a phone, a green
  dot) while connected.
- **Sharing.** Invite people by link or by email, as an editor (can change items) or a viewer
  (can only look). The board list has "My boards" and "Shared with me", and a ⋮ menu on every
  board: the owner renames, invites and archives, everyone else can leave. On a shared board each
  item shows the initials of whoever added it.
- **Archive instead of delete.** Boards and categories are archived, not deleted: they can be
  restored, and are removed for good only after a retention period you choose (30 days by
  default). A deleted item can be brought back with "Undo".
- **Attachments.** Attach photos (also straight from a phone's camera, HEIC included), PDFs, `.txt`
  and `.md` files to an item. Pictures show as thumbnails and the other files open in the browser.
- **Checklists.** An item can hold a checklist; its tile shows how many entries are done, in
  green once all of them are.
- **Notifications.** A bell in the header counts what happened on your boards while you were away:
  members joining, leaving or removed, role changes, archived and restored boards, moved
  categories.
- **Sorting.** Your own order for items: a default in your account, and a different one for any
  category. Nobody else's view changes.
- **Accounts.** Sign-up (can be closed), sign-in with a lockout after 10 wrong passwords,
  optional two-factor authentication (an authenticator app, plus 10 recovery codes to save as a
  file), a display name, password reset by email, and a list of where you are signed in, with
  "Sign out other devices".
- **Admin panel.** Statistics, and a user list with search: create an account with a temporary
  password, send a password reset, reset 2FA, disable or delete an account.
- **Made for phones.** Responsive layout, dark mode that follows the system, and it can be added
  to the home screen. "New category" opens a dialog that is easy to use with a thumb. All scripts
  and styles are served by the app itself, without CDNs.
- **Self-hosted.** One docker compose stack (the app and PostgreSQL), with an optional Caddy
  reverse proxy for automatic HTTPS.

## What's new in v2.1

v2.1 adds these to v2.0, in this order:

1. **Recovery codes as a file.** The screen with the 10 two-factor recovery codes has "Save as
   .txt" and "Copy". The file, `easytodo-recovery-codes.txt`, holds the account's email, the date
   and the codes. It is made in the browser: the codes are not stored on the server.
2. **A ⋮ menu on every board.** Managing a board is no longer on the board's page. On the board
   list each board has a ⋮ button. The owner finds Rename, Invite and Archive there. Invite is
   one page, "Invite and members": the invitations and the members with their roles (it replaces
   "Members"). Someone who was invited has only "Leave board".
3. **Archive instead of delete.** For boards and categories "Archive" replaces "Delete": the item
   disappears for everybody and its data stays. Archived boards are under "Archived (n)" below
   the board list, archived categories under "Archived categories" on the board. Each has
   "Restore" and "Delete permanently", which asks you to type the name. Only a board's owner
   archives or restores it; an editor or the owner archives, restores and deletes categories.
   Archived items are deleted for good after 7, 30, 90 or 365 days, or never, as set under Account
   → "Delete archived items after" (30 days by default). For a board and its categories the
   board owner's setting counts. The app does this once a day by itself ([Archive and
   retention](#archive-and-retention)). A single item is still deleted at once, with an "Undo"
   button in the message for 5 seconds.
4. **My boards and Shared with me.** The board list has the two sections. A board of yours with
   at least one other member has a "Shared" badge. A board someone invited you to shows the
   owner's email and your role.
5. **Move a category to another board.** "Move to board…" in the category's menu lists the boards
   where you are an editor or the owner (and you must be an editor of the one it comes from). The
   category goes to the end of the other board with its items, checklists, attachments and
   authors, and both boards update on every open screen. If the other board already has a
   category with that name, the dialog asks for a new one, "Dairy (2)" for "Dairy".
6. **Who added an item.** On a board with more than one member each item has a small coloured
   circle with the initials of whoever added it (each person keeps their colour). The item's
   details say "Added by …" with the date. Items from before v2.1 have no author, and a board with
   one member looks as before. Account → "Display name" is optional and defaults to the part of
   the email before the @.
7. **Notifications.** The bell next to Admin, Account and Sign out counts the unread ones, and the
   panel updates without reloading. It lists the newest 20 of the last 30 days: someone joined
   your board or left it; you were removed from a board, or your role on it changed; the owner
   archived or restored a board you belong to; someone moved a category to or from a board you
   share. There are none about items, no mails and no push messages.
8. **"New category" dialog.** The button opens a dialog with the name and "On top" or "At the
   bottom" (the default), and the cursor then goes to "Add item" in the new category. The form at
   the bottom of the board is gone. It works the same on a phone and on a desktop.
9. **Sorting.** Account → Sorting sets the default order of items for all your boards: "Open items
   first" on or off, then the name, the date added or the date modified, ascending or descending.
   The default is the old order: open items first, then by name. The ⇅ button on a category sets
   another order for it, only for you, and "Use my default" takes it back. The item's details show
   when it was added and last modified.
10. **Attachments.** `.txt`, `.md`, pictures (JPEG, PNG, WebP, GIF, and HEIC from an iPhone) and PDF
    files, recognised by their content, not only their extension. On a phone you can take a photo
    or pick a file. The details show pictures as thumbnails, open a PDF in the browser, and show
    `.txt` and `.md` files as safe text (Markdown without HTML). A paperclip with the number of
    files is on the item's tile. A viewer can look at and download files; an editor can also add
    and delete them. Limits: 10 MB a file, 20 files an item and 2 GB for the instance
    (`EASYTODO_ATTACHMENT_MAX_BYTES`, `EASYTODO_ATTACHMENTS_PER_ITEM`,
    `EASYTODO_ATTACHMENTS_TOTAL_BYTES`). The files live in their own docker volume, `attachments`,
    not in the database, and every download goes through the app, which checks the board access.
    The volume belongs in the backup ([Backup and restore](#backup-and-restore)).
11. **Checklists.** An item's details can hold a checklist of up to 100 entries to tick, edit,
    delete and drag into another order. The item's tile shows a counter such as "5/20", which
    turns green when every entry is ticked. All of it updates live. Ticking the last entry does
    not tick the item: you do that yourself.

Also changed since v2.0, from its review: rate limits count an IPv6 address by its /64 network,
password guesses on the account pages are limited per account, and an account gets at most 3
reset mails an hour; the CSRF token in a page is a fresh mask each time; a password change gives
the device that made it a new session and signs out the others; the access log hides the tokens of
invite and reset links (`/invite/[redacted]`); the health check is time-bound, and the container
warns at start when `EASYTODO_FORWARDED_ALLOW_IPS` trusts everyone but the port is not bound to
127.0.0.1 ([docs/deployment.md](docs/deployment.md)).

One limit: a long word in a category title can still wrap mid-word where the title is narrow. On a
phone the title has 240 px less than the screen is wide (120 px at 360 px, 135 px at 375 px, 519 px
at 767 px: landscape phones have one column, so room), from 768 px up to about 775 px it has 140 to
143 px (two columns), and from 1280 px it has 141 px (three columns). A 12-letter word such as
"gospodarstwa" is about 125 px wide in Inter, 122 px in Liberation Sans and 143 px in DejaVu Sans
(bold, 18 px), the default font of some Linux desktops: so in DejaVu Sans it can split on phones
narrower than 383 px, from 768 to 774 px and from 1280 px up, and in Inter on phones narrower than
365 px; longer words need more room than that.

### Upgrading from v2.0

There is nothing to convert by hand: the database migrates itself when the container starts, and
the `attachments` volume creates itself. In order:

1. **Back up.** Take the backup from [Backup and restore](#backup-and-restore) and keep the dump
   until you have used v2.1 for a while: it is one way back. v2.0 has no attachments, so the dump
   is all there is to save.
2. **Update.** Run `git pull && docker compose up -d --build` (with the proxy: `docker compose
   --profile proxy up -d --build`). When the container starts it runs `alembic upgrade head`,
   which brings the database to `0002`: no existing data is changed or removed. `docker compose
   exec app alembic current` shows `0002 (head)`.
3. **Add the `attachments` volume to your backup.** docker compose creates it (`easytodo_attachments`)
   on the first start. It is empty, so nothing is lost yet, but a backup that only holds the
   database loses every file attached from now on: add the `tar` line of [Backup and
   restore](#backup-and-restore) to your backup job, next to the `pg_dump` one.
4. **Clean-up is built in.** Nothing needs scheduling: the app removes archived items past their
   retention, items deleted more than 10 minutes ago, old notifications and files that no item has
   any more by itself, 5 minutes after it starts and then every 24 hours
   (`EASYTODO_PURGE_INTERVAL_HOURS`).
5. **Check the retention of every owner.** What counts is the setting of a board's **owner**,
   Account → "Delete archived items after", 30 days for every existing account. Someone who wants
   archived boards kept longer, or forever, sets it there.
6. **What looks different.** Existing items have no author, so they show no initials; items
   added from now on do, on boards with more than one member. Every account starts with the old
   sort order (open items first, then by name) and no display name. "Delete board" is "Archive" in
   the board's ⋮ menu on the board list, "Delete category" is "Archive category" in the
   category's ⋮ menu, and "Members" is now the page "Invite and members" (⋮ on the board list →
   "Invite").

**Going back to v2.0.** Stop the app, bring the database back to the v2.0 schema with the
v2.1 image, then start v2.0:

```sh
docker compose stop app
docker compose run --rm --entrypoint alembic app downgrade 0001
git checkout v2.0.0
docker compose up -d --build
```

The downgrade deletes what v2.1 added: authors, sort settings, checklists, attachments (their
rows; the files stay in the `easytodo_attachments` volume), notifications, display names and
retention settings. It deletes no board, category or item: archived boards and categories, and
items that were deleted but not yet purged, are there again as ordinary ones. A category that was
archived under a name another category of its board has since taken comes back as, say, "Dairy
(archived 12)", because v2.0 wants the names of a board's categories to be different. To get back
to v2.1 later, `git checkout` your branch and `docker compose up -d --build`. Or restore the dump
you took before the update into an empty database ([Backup and restore](#backup-and-restore)):
then everything made since is gone.

## Quick start

You need Docker with Compose (Docker 25 or newer), `git` and `openssl`, in a Linux or macOS shell
(on Windows, use WSL).

```sh
git clone https://github.com/DaveKadziola/easytodo-grocery-list.git
cd easytodo-grocery-list
cp .env.example .env
sed -i.bak -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" \
    -e "s/^EASYTODO_SECRET_KEY=.*/EASYTODO_SECRET_KEY=$(openssl rand -hex 32)/" .env && rm .env.bak
docker compose up -d --wait
docker compose exec app python -m easytodo.cli create-admin --email you@example.com
```

- The `sed` line puts a random database password and a random `EASYTODO_SECRET_KEY` into `.env`.
  Run it only once, before the first start: the database keeps the password it was created
  with.
- The first `docker compose up` builds the image, which takes a few minutes. `--wait` returns once
  the app is up and has created its database tables.
- `create-admin` asks for a password twice (10 to 256 characters). For an existing account it only
  adds admin rights; a disabled account stays disabled until an admin enables it at
  `/admin/users`.

Open <http://localhost:8000> and sign in with that email and password. To use the app from your
phone, see "Phone on the home network" in the checklist below.

`docker compose down` stops the app and keeps the data. `docker compose up -d` starts it again.

On a server with a public IP address (a VPS, say), set `EASYTODO_BIND=127.0.0.1` in `.env`
before the first start and follow [docs/deployment.md](docs/deployment.md) (b) or (c): otherwise
the app is reachable from the internet over plain HTTP, with registration open, and ports that
Docker publishes bypass firewalls such as ufw.

## Manual test checklist

A walk through everything by hand on a fresh install. The steps with two people need two
browsers, or a normal and a private window, each signed in as someone else.

1. **Registration.** Sign out, choose "Create an account" and create a second account, e.g.
   `bob@example.com`. It starts with a board named "My list". With
   `EASYTODO_REGISTRATION_OPEN=false` in `.env` (then `docker compose up -d`), `/register` is
   closed (404), except from a valid invite link (step 4): its sign-in page still offers
   "Create an account". An e-mail invite works only for its own address. A link invite lets
   anyone who has the link create an account until it expires (7 days) or is revoked, so revoke
   links you no longer need.
2. **Two-factor authentication.** Account → "Set up two-factor authentication". Scan the QR code
   with an authenticator app (or type the key shown under it), enter your "Current password" and
   the 6-digit "Code from the app", choose "Turn on two-factor authentication", and keep the
   recovery codes. Sign out and in again: after the password the app asks for a code. A
   recovery code works instead, once.
3. **Password reset.** Sign out → "Forgot password?" → enter the email. With the default
   `EASYTODO_MAIL_BACKEND=console` nothing is sent: the mail, link included, is written to the
   app's log. Show the link with:

   ```sh
   docker compose logs app | grep /password/reset/
   ```

   Open it (it works once, for 60 minutes) and choose a new password. That signs the account out
   everywhere.
4. **Sharing.** As the owner, open the board list, choose ⋮ on the board → Invite. On the
   "Invite and members" page, under "Invite someone", pick the role "Viewer", then either leave
   the email empty to get a link (it is shown once: copy it) or enter `bob@example.com` (the
   console backend sends no mail: the page then shows the invite link to pass on, which only
   Bob's account can use). Open the link in Bob's browser and choose "Join board". As a viewer,
   Bob sees the board without edit controls, and it is listed under "Shared with me". Change his
   role to editor on the "Invite and members" page and he can edit. As Bob, choose ⋮ on the
   board in his list → "Leave board"; invite him again, and then remove him from the "Invite and
   members" page.
5. **Real time with two browsers.** Bob is off the board after step 4, so use the owner on a
   second device (a private window signed in as the owner), or invite Bob again first (step 4)
   and open the board as Bob. Open the same board in both browsers; each header shows "Live".
   Add, tick, rename and drag items in one: the other follows without reloading. Then take one of
   them offline (airplane mode on a phone, or developer tools → Network → Offline), change
   something in the other, and go back online: the first one catches up by itself, without a
   reload (meanwhile its header may show "Reconnecting…"). On an iPhone or iPad, check the same
   in Safari: the board shows "Live" (the green dot), and a change made on another device appears
   without reloading.
6. **Admin panel.** As the admin: "Admin" in the header shows the statistics, "Manage users" the
   accounts. Create an account (its temporary password is shown once, and it must choose a new one
   at its first sign-in), send a password reset (link in the log, as in step 3), reset 2FA,
   disable an account (it is signed out at once) and delete one.
7. **Phone on the home network.** Find the computer's LAN address (Linux: `hostname -I`, macOS:
   `ipconfig getifaddr en0`), say `192.168.1.20`. In `.env` set
   `EASYTODO_PUBLIC_URL=http://192.168.1.20:8000`, then run `docker compose up -d` to apply it.
   Open `http://192.168.1.20:8000` on the phone, on the same Wi-Fi. The app accepts form
   submissions only from the address in `EASYTODO_PUBLIC_URL` and those in
   `EASYTODO_EXTRA_ORIGINS`, so at `http://localhost:8000` signing in and saving now fail with
   "This page was opened at a different address.", and a board page there shows "Offline" with
   a message naming the address to use. To keep `http://localhost:8000` working as well, also
   set `EASYTODO_EXTRA_ORIGINS=http://localhost:8000` (more addresses: comma-separated). If the
   phone cannot connect, allow incoming TCP port 8000 in the computer's firewall. The
   browser's "Add to Home Screen" installs it like an app.
8. **Recovery codes as a file.** On the recovery codes screen (the end of step 2, or Account →
   Two-factor authentication → your "Current password" → "Get new recovery codes") choose "Save
   as .txt": the browser saves `easytodo-recovery-codes.txt`, with the account's email, the date
   and the codes. "Copy" puts the same text on the clipboard; paste it somewhere to see it. On a
   phone: the file ends up in the browser's downloads (on an iPhone, in Files).
9. **The ⋮ menu on a board.** As the owner, open the board list. ⋮ on a board shows Rename,
   Invite and Archive; open a board: its page has no buttons for managing the board. Share it
   with Bob again, as an editor (step 4); this is the board of steps 12 to 18. As Bob, ⋮ on
   that board in his list shows only "Leave board".
10. **Archive and delete for good.** As the owner, create a board named `Łosoś` and choose ⋮ →
    Archive. It disappears from the list, for Bob too if it was shared, and "Archived (1)" shows
    under the list. Open it: the page gives the date and the day it is deleted for good
    ([Archive and retention](#archive-and-retention)); "Restore" brings it back. Archive it
    again and choose "Delete permanently": the box asks you to type the name. Typing `losos` or
    nothing deletes nothing; `Łosoś`, with the Polish letters, deletes the board. Do the same for
    a category (its ⋮ menu → "Archive category", then "Archived categories" on the board). Under
    Account → "Delete archived items after" pick another time and "Save archive settings".
    Finally delete an item: a message with "Undo" appears for 5 seconds, and "Undo" brings the
    item back.
11. **My boards and Shared with me.** The owner's list has the section "My boards", and a board
    that Bob has joined carries the badge "Shared". Bob's list has "Shared with me", where the
    board shows the owner's email and Bob's role.
12. **Move a category to another board.** As the owner, take the board you shared with Bob in step
    9, where he is an editor; it is called "Groceries" here. Give it the categories "Dairy" and
    "Bakery" with a few items each, a checklist on one Dairy item and a file on another (steps 17
    and 18), and make a second board "Party" with a category "Dairy" as well. Open the category's ⋮
    menu on "Groceries" → "Move to board…", pick "Party" and choose "Move". The dialog says "That
    board already has a category with this name." and fills in "Dairy (2)"; choose "Move" again. The
    category is the last one on "Party", with its items, checklist and files, and "Groceries" no
    longer has it: it keeps "Bakery", which the steps after this one use. Have both boards open in
    two browsers: both change without reloading. Bob's dialog lists only the boards where he is an
    editor or the owner, and he can move a category only from a board where he is an editor.
13. **Who added an item.** On the board shared with Bob, add an item as Bob: a coloured circle
    with his initials ("B" for bob@example.com) shows on it for both of you. Your own items on
    that board, including the ones you added before sharing it, show your circle as well. Open
    the item: it says "Added by bob" with the date, and when it was last modified. Bob sets
    Account → "Display name" to "Bob Marley" and chooses "Save name": the circle reads "BM".
    Remove Bob (step 4) and the board has one member again: no item shows a circle, and the
    details say only "Added" with the date. Items from before v2.1 have no author and never show
    one ([Upgrading from v2.0](#upgrading-from-v20), step 6).
14. **Notifications.** Bob is off the board since step 13. As the owner, watch the bell in the
    header while Bob joins it again by an invite link (a viewer, step 4): its counter goes up
    without reloading, and the panel says that Bob joined. Bob leaves the board: another
    notification. Invite him once more, again as a viewer, and then, as the owner, change his
    role to editor: the bell of Bob's browser shows that. Archive the board: Bob's bell shows
    it, and it is gone from his list. Restore it from "Archived" (step 10) and Bob has it
    again, still as an editor. Opening the panel reads everything: the counter goes back to
    zero, on the user's other devices too.
15. **"New category".** On a phone: open a board. The bar at the bottom has "Boards" and "New
    category". Tap "New category": a dialog asks for the name and offers "At the bottom"
    (chosen) and "On top". Type a name, choose "On top" and "Add category": the category is the
    first on the board and the cursor is in its "Add item" box, ready to type. On a desktop
    browser the same button is in the board's header.
16. **Sorting.** Account → Sorting: set "Sort by" to "Date added", "Order" to "Z–A / newest first"
    and "Save sorting"; every category lists its items that way (open ones first, as long as "Open
    items first" is ticked). In one category of the board shared with Bob, ⇅ → another "Sort by" →
    "Apply": the button is marked as custom (its tooltip says "Custom order"), and "Use my default"
    takes it back. As Bob, set a different order on the same category: the owner's view of it stays
    as it was. The item's details give when it was added and last modified.
17. **Attachments.** Open an item of the board shared with Bob and, under "Attachments", "Add a
    file" → "Upload": a `.txt` file, a `.md` file with a heading and a link, a JPEG or PNG and a
    PDF. The picture has a thumbnail, the PDF opens in a new tab, the other two open on a page of
    their own (the Markdown's heading is a heading, and a `javascript:` link in it is not a link).
    The item's tile shows a paperclip with the number of files. These must be refused with a message
    and leave nothing behind: a file of 11 MB ("Files can be at most 10 MB."), an `.exe` renamed to
    `photo.png` ("Only images, PDF, .txt and .md files can be attached."), and a `.txt` file that is
    not UTF-8 text ("Text files must be saved as UTF-8."). Make Bob a viewer on the "Invite and
    members" page: as Bob, the files can be opened and downloaded, but there is no "Add a file" and
    no ✕. Run `docker compose down` and `docker compose up -d`: the files are still there. On a
    phone: "Add a file" offers the camera, so take a photo with it; on an iPhone, a photo kept in
    HEIC works too, and shows as a picture.
18. **Checklists.** Open an item → "Checklist" → "Add entry" and add three entries. Tick one, edit
    another (Enter saves it), delete the third with ✕, then add a few more and drag one by its
    handle ⠿, or focus the handle and press ↑ or ↓. The item's tile shows "1/3" or so. Tick every
    entry: the counter turns green, and the item itself is not ticked. Have the board open in a
    second browser: the counter follows without reloading.

## Configuration

Everything is set in `.env`, which docker compose reads; [.env.example](.env.example) explains
each line. After a change, `docker compose up -d` applies it.

| Variable | Default | What it does |
| --- | --- | --- |
| `EASYTODO_DATABASE_URL` | required | PostgreSQL URL, `postgresql+asyncpg://user:password@host:port/db`. `.env.example` points it at the compose `db` service, with `POSTGRES_PASSWORD` filled in. |
| `EASYTODO_SECRET_KEY` | required | At least 32 characters (`openssl rand -hex 32`). Encrypts the 2FA secrets in the database: keep it unchanged, and keep a copy in a safe place apart from your backups. |
| `EASYTODO_PUBLIC_URL` | `http://localhost:8000` | The address people type in the browser, and the one links in mails and invites use. The app accepts form submissions (CSRF check) and live updates only from its origin and from `EASYTODO_EXTRA_ORIGINS`. An `https://` address also makes the session cookie `Secure`. |
| `EASYTODO_EXTRA_ORIGINS` | none | More addresses the app is opened at, comma-separated, e.g. `http://localhost:8000, http://192.168.1.20:8000`: form submissions and live updates work there too. Mail and invite links still use `EASYTODO_PUBLIC_URL`. An `https://` `EASYTODO_PUBLIC_URL` cannot be combined with `http://` extras (the app refuses to start): its session cookie is `Secure`, so the browser never sends it to an http address and signing in there would loop. |
| `EASYTODO_REGISTRATION_OPEN` | `true` | `false` closes `/register`, except from a valid invite link. An e-mail invite works only for its own address; a link invite lets anyone who has the link create an account until it expires (7 days) or is revoked, so revoke links you no longer need. Admins can still create accounts. |
| `EASYTODO_SESSION_TTL_DAYS` | `30` | How long a sign-in stays valid, in days. |
| `EASYTODO_PURGE_INTERVAL_HOURS` | `24` | How often, in hours, the app deletes what is past its retention (see [Archive and retention](#archive-and-retention)). `0` turns it off. |
| `EASYTODO_ATTACHMENTS_DIR` | `data/attachments` | Where the files attached to items are kept (images, PDF, `.txt`, `.md`). In the image it is `/app/backend/data/attachments`, on the `attachments` volume: leave it as it is with docker compose. |
| `EASYTODO_ATTACHMENT_MAX_BYTES` | `10485760` | The largest file that can be attached, in bytes (10 MiB). |
| `EASYTODO_ATTACHMENTS_PER_ITEM` | `20` | How many files one task can have. |
| `EASYTODO_ATTACHMENTS_TOTAL_BYTES` | `2147483648` | All attached files of the instance together, in bytes (2 GiB). Beyond it no file is accepted until some are deleted; the admin page shows how much is used. |
| `EASYTODO_MAIL_BACKEND` | `console` | `console` writes mails to the app's log and sends nothing. `smtp` sends them (see SMTP below). |
| `EASYTODO_SMTP_HOST` | none | SMTP server. |
| `EASYTODO_SMTP_PORT` | `587` | SMTP port. |
| `EASYTODO_SMTP_USER` | none | SMTP login. Leave it out for a server without login. |
| `EASYTODO_SMTP_PASSWORD` | none | SMTP password. |
| `EASYTODO_SMTP_FROM` | `EasyToDo <noreply@localhost>` | Sender of the mails. |
| `EASYTODO_SMTP_STARTTLS` | `true` | Upgrade the connection with STARTTLS. `false` never upgrades. |
| `EASYTODO_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted. Name only your reverse proxy ([docs/deployment.md](docs/deployment.md)). |
| `EASYTODO_ENV` | `dev` (`prod` in `.env.example`) | `dev`, `test` or `prod`. A label only: nothing depends on it yet. |
| `EASYTODO_PORT` | `8000` | Host port docker compose publishes the app on. Use the same port in `EASYTODO_PUBLIC_URL`. |
| `EASYTODO_BIND` | every interface | Host address for that port. `127.0.0.1` with the reverse proxy. |
| `EASYTODO_WORKERS` | `1` | Uvicorn worker processes. The rate limits live in each worker's memory, so N workers allow N times as many attempts (and a restart resets them): keep `1`, which is plenty for a household, on an install reachable from the internet. |
| `EASYTODO_DOMAIN` | none | Domain name for Caddy's certificate (only with the `proxy` profile). |

`.env` also holds two settings for the other containers: `POSTGRES_PASSWORD` (the `db` service's
password, used in `EASYTODO_DATABASE_URL`) and `ACME_EMAIL` (Caddy's contact address for Let's
Encrypt, only with the `proxy` profile).

## HTTPS and deployment

For use over the internet, put the app behind HTTPS: the optional Caddy reverse proxy
(`docker compose --profile proxy up -d`) gets and renews a Let's Encrypt certificate for your
domain. [docs/deployment.md](docs/deployment.md) covers the home network, your own domain with
Caddy, Cloudflare Tunnel or Tailscale Funnel, and a checklist for production (passwords, closing
registration, mail, logs, backups, updates).

## SMTP

Password resets and email invitations need mail. With the default `console` backend, mails only
go to the app's log. To send them, set in `.env`:

```sh
EASYTODO_MAIL_BACKEND=smtp
EASYTODO_SMTP_HOST=smtp.example.com
EASYTODO_SMTP_PORT=587
EASYTODO_SMTP_USER=you@example.com
EASYTODO_SMTP_PASSWORD=your-smtp-password
EASYTODO_SMTP_FROM=EasyToDo <you@example.com>
```

docker compose fills in `${...}` in `.env` values, so wrap a value that contains a `$` in single
quotes (`EASYTODO_SMTP_PASSWORD='pa$$word'`); otherwise it is silently changed and sign-in to the
SMTP server fails.

Then run `docker compose up -d`. The app connects on port 587 and upgrades with STARTTLS. A
server that needs TLS from the start (port 465) is not supported. For a relay without TLS (a
local server on port 25), set `EASYTODO_SMTP_STARTTLS=false`. To test it, request a password
reset for your own account. If no mail arrives, `docker compose logs app` shows "Could not send
mail to ..." with the reason.

### Proton Mail

Proton Mail takes mail from apps with an SMTP token, not with the account password. Create one in
Proton under Settings → IMAP/SMTP → SMTP tokens (this needs a paid plan with a custom domain),
for the address the app sends from. Then, in `.env`:

```sh
EASYTODO_MAIL_BACKEND=smtp
EASYTODO_SMTP_HOST=smtp.protonmail.ch
EASYTODO_SMTP_PORT=587
EASYTODO_SMTP_STARTTLS=true
EASYTODO_SMTP_USER=todo@your-domain.example
EASYTODO_SMTP_PASSWORD=<the SMTP token>
EASYTODO_SMTP_FROM=EasyToDo <todo@your-domain.example>
```

The From address must be the token's address (the one in `EASYTODO_SMTP_USER`). If the token
contains a `$`, wrap it in single quotes, as explained above.

## Archive and retention

An archived board (only its owner archives or restores it) or category is hidden from everyone
until it is restored from the archive, or deleted for good. How long it is kept is the setting of
the **owner of the board**, under Account → "Delete archived items after" (7, 30, 90 or 365 days,
or never), for a category too, whoever archived it. The archive page shows the date.

The app deletes by itself: 5 minutes after it starts, and then every
`EASYTODO_PURGE_INTERVAL_HOURS` hours (24 by default), so something is gone at most one interval
after the date the archive shows. The same run also deletes:

- tasks that were deleted more than 10 minutes ago (until then they are only hidden, for "Undo"),
- notifications older than 30 days,
- attached files that no task has any more. A file written less than an hour ago is left alone
  (it may be an upload in progress). The files are cleaned only if the database holds at least
  one attachment: with an empty `attachments` table the purge keeps the files and writes a
  warning to the log, because that is an empty or a not yet restored database next to a volume of
  files (see Backup and restore below).

When it has deleted something, it writes one line to the log (`docker compose logs app`), such as
`Purged: 1 boards, 0 categories, 2 items, 0 notifications, 3 files.` Set the interval to `0` to
turn it off. To run it now, or from cron when it is off:

```sh
docker compose exec app python -m easytodo.cli purge
```

## Backup and restore

The data lives in two volumes: `pgdata` (the database, in the `db` service) and `attachments` (the
files attached to items, in the `app` service). Back up both, from the repository root, into
`backups/` (git-ignored; the dumps are readable by your user only, as they hold every account's
password hash), and copy the backups off the server. Take them at about the same time: the
database says which files exist.

```sh
mkdir -p backups
(umask 077 && docker compose exec -T db pg_dump -U easytodo -d easytodo -Fc > backups/easytodo-$(date +%F).dump)
(umask 077 && docker compose exec -T app tar -C /app/backend/data/attachments -cf - . > backups/attachments-$(date +%F).tar)
```

Restore a dump and the files of the same day, in this order: stop the app, restore the database,
restore the files (the app stays stopped), start the app:

```sh
docker compose stop app
docker compose exec -T db pg_restore -U easytodo -d easytodo --clean --if-exists < backups/easytodo-2026-10-21.dump
docker compose run --rm --no-deps -T --entrypoint tar app -C /app/backend/data/attachments -xf - < backups/attachments-2026-10-21.tar
docker compose start app
```

A file the restored database does not know (it was attached after the dump) is removed by the
app's periodic purge of files without a row (every 24 hours by default; with
`EASYTODO_PURGE_INTERVAL_HOURS=0` it is off, and it runs only when you start it with
`docker compose exec app python -m easytodo.cli purge`); `tar` keeps the files' old modification
times, so a restore does not make them look new. A row whose file is missing from the backup shows
"This attachment no longer exists." instead of the file.

**A dump taken before the update to v2.1 does not go over a v2.1 database with `--clean`.** It
does not know the tables that migration `0002` added, so `--clean` leaves them where they are,
the restore ends with errors, and the database says it is at `0001` while it has the tables of
`0002`: the next start fails when the migration tries to create them again. Restore such a dump
into an empty database (this is also how you go back to v2.0 with the data it had, see [Upgrading
from v2.0](#upgrading-from-v20)):

```sh
docker compose stop app
docker compose exec -T db dropdb -U easytodo --force easytodo
docker compose exec -T db createdb -U easytodo easytodo
docker compose exec -T db pg_restore -U easytodo -d easytodo < backups/easytodo-2026-10-21.dump
git checkout v2.0.0   # for v2.0; to stay on v2.1, skip this line
docker compose up -d --build
```

On v2.1 the app then runs `0002` on the restored data at its start, as in an update.

Keep a copy of `EASYTODO_SECRET_KEY` too, in a safe place but not next to the dumps (a password
manager, say): without it, the 2FA secrets in a restored database cannot be read, and with it
anyone holding a dump can read them. Never run `docker compose down -v`: `-v` deletes the
database and attachments volumes.

## Local development

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed), Docker for the
database and `make`. Node.js is not needed. From the repository root:

```sh
make test-db          # PostgreSQL 17 on localhost:54329, data kept in memory
docker compose -f compose.test.yml exec db createdb -U easytodo easytodo_dev
cd backend && uv sync && cd ..
make css              # downloads the Tailwind CLI into bin/, builds frontend/static/css/app.css
printf 'EASYTODO_DATABASE_URL=postgresql+asyncpg://easytodo:easytodo@localhost:54329/easytodo_dev\nEASYTODO_SECRET_KEY=%s\n' \
    "$(openssl rand -hex 32)" > backend/.env
cd backend && uv run alembic upgrade head && cd ..
make run              # http://localhost:8000, restarts on code changes
```

- The app reads `backend/.env` when it runs from `backend/`. Keep `EASYTODO_PUBLIC_URL` out of it:
  the default `http://localhost:8000` is what `make run` serves, and one test checks that default.
- The test database container keeps its data in memory, so after it restarts, run the
  `createdb` and `alembic upgrade head` lines again.
- Run `make css` again after adding Tailwind classes to the templates. The Docker image builds the
  CSS by itself.
- The tests use their own database (`easytodo_test` in the same container) and empty it after
  each test. It must be UTF8, as the one in that container is (`postgres:17-alpine` creates it so):
  names are sorted in Polish alphabetical order with the ICU collation `pl-x-icu`, which only a
  UTF8 database has, and the tests of that order fail on a `SQL_ASCII` one (`createdb -E UTF8
  -T template0 ...` makes a UTF8 database). The app itself falls back to the database's own order
  and logs a warning when it starts on a database without that collation.
- The browser tests render every page in the font Inter, because where a word wraps depends on
  the font. On Linux, `backend/tests/e2e/fonts.conf` makes Chromium use Inter whatever your
  system's default is: install it first with `sudo apt-get install fonts-inter` (Debian, Ubuntu;
  CI does the same). On macOS, Chromium does not use fontconfig, so nothing is pinned: the suite
  only requires Inter to be installed (`brew install --cask font-inter`) and renders the pages in
  the system's own font, so a layout test can behave differently there than in CI. Without Inter
  the suite stops and says so.

```sh
make test             # tests, without the browser tests
cd backend && uv run playwright install chromium && cd ..   # once, before the first make e2e
make e2e              # browser tests (Playwright, Chromium); builds the CSS first
make lint             # ruff
```

`scripts/bench.py` measures response times; `--realistic` seeds a shared board with checklists and
files. [docs/performance.md](docs/performance.md) has the v1-to-v2 and the v2.0-to-v2.1
comparisons and how to repeat them. `scripts/docker-smoke.sh` is the check CI runs against the
running compose stack (`sh scripts/docker-smoke.sh`, on a stack you can throw away).

## Database migrations

The schema is managed by Alembic (`backend/migrations/`). **Every model change is a new
migration.** After changing the models in `backend/src/easytodo/models/`:

```sh
cd backend
uv run alembic upgrade head          # bring the dev database up to date first
uv run alembic revision --autogenerate -m "add item quantity"
uv run alembic upgrade head
uv run alembic check                 # "No new upgrade operations detected."
```

Read the generated file in `backend/migrations/versions/` before committing it: autogenerate
does not see everything (renames, CHECK constraints on existing tables, a partial index's
`WHERE`). CI runs `alembic upgrade head` and `alembic check`, and the test suite fails when the
models and the migrations differ, server defaults included.

The container applies the migrations itself: every time it starts, `deploy/entrypoint.sh` runs
`alembic upgrade head` before starting the app (retrying while the database starts up). An update
is therefore `git pull && docker compose up -d --build`, after a backup.

## Architecture

The backend is FastAPI (async), which serves the pages, the HTML fragments and the WebSockets in
one process, with SQLAlchemy and asyncpg on PostgreSQL. The browser side is server-rendered HTML
with HTMX, the CSP build of Alpine.js and Tailwind: each action sends a form and swaps in the HTML
that comes back, so there is no client-side state to keep in sync, and no Node.js anywhere. Real
time runs over PostgreSQL `LISTEN`/`NOTIFY`: every change sends a `pg_notify` inside its
transaction, so an event leaves only after the commit, and it reaches every Uvicorn worker
without Redis. Events are thin (a type and ids): the page fetches the HTML it needs through the
normal, authorised routes, and after every (re)connect it reloads the whole board, so no event
log is needed. Besides the board's socket (`/ws/boards/{id}`) each page holds one for the user
(`/ws/user`), which only says that the notifications changed. Sign-in uses server-side sessions in
an httpOnly cookie with a CSRF token instead of JWTs, so signing out, a password reset or an admin
ends a session at once, its WebSockets included.

Attached files are not in the database: they are files in `EASYTODO_ATTACHMENTS_DIR` (the
`attachments` volume), under random names, and each download goes through the app, which checks
the member's access to the board first. The database holds the file's name, type and size. The
app also deletes by itself, in a background task (`purge`, every `EASYTODO_PURGE_INTERVAL_HOURS`
hours): archived boards and categories past their retention, deleted items, old notifications
and files that nothing refers to any more. `python -m easytodo.cli purge` runs it by hand.

- `backend/`: the Python package (`src/easytodo/`), migrations and tests.
- `frontend/`: Jinja templates, static files (vendored JS libraries) and the Tailwind source.
- `deploy/`: Dockerfile, entrypoint and Caddyfile.
- `scripts/`: the benchmark (`bench.py`) and the compose smoke test (`docker-smoke.sh`).
- `docs/`: deployment, migration from v1, performance.

## Releases

Work reaches `develop` through PRs with green CI. When `develop` is ready, a release PR merges it
into `main` with a merge commit, and the operator merges that PR. The CI run on `main` then gives
the next `vX.Y.Z` tag and a GitHub release; the releases are the changelog. Rulesets: `develop`
takes only PRs with green `test`, `e2e` and `docker`, `main` only PRs, and neither allows a force
push or deletion.

The `release` job in `.github/workflows/ci.yml` runs `scripts/release-notes.py`, which lists the
PRs merged since the newest tag and names the release PR in the header. A title is
`type(scope): subject`: `!` after the type or `BREAKING CHANGE` in the body bumps the major
version, any `feat` the minor, anything else the patch. A title in another form lands under Other
and counts as a patch. A wrong version or text is fixed by editing the release on GitHub; the job
never moves a tag. Preview of the next release:
`python3 scripts/release-notes.py --target origin/develop`.

## License

[MIT](LICENSE)

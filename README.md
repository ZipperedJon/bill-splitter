# Bill Splitter

A small self-hosted web app for splitting costs with friends — dinners, trips,
Airbnbs, the household gas bill. Runs on a Raspberry Pi, keeps its data in one
SQLite file, and updates itself from GitHub.

Built to sit alongside another project on the same Pi, so it listens on
**port 9100** rather than 9000.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ZipperedJon/bill-splitter/main/install.sh | bash
```

That one command installs git and Python if they are missing, clones the repo,
builds a virtualenv, generates a secret key, installs a systemd service, and
starts it. Then open `http://<your-pi>:9100`.

**The first account created becomes the admin.** Nobody else can sign in until
that admin approves them.

Re-running the installer upgrades in place and never touches your database.
`./install.sh --uninstall` removes the service and leaves your data alone.

<details>
<summary>Running from a checkout instead</summary>

```bash
git clone https://github.com/ZipperedJon/bill-splitter.git
cd bill-splitter
./install.sh
```

Or without any service at all:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m app.main      # http://localhost:9100
```
</details>

## What it does

**Splitting, three ways.** Split evenly; split by shares (a couple sharing an
Airbnb room counts as 2); or split *itemized* — enter each line off the receipt
and tick who is on it. Tax and tip are then apportioned in proportion to what
each person actually ordered, so the person who had a salad is not subsidising
the steak.

**Tax and tip, percentage or exact.** Either type a percentage or type the exact
figure printed on the receipt. Tip can be calculated on the pre-tax subtotal or
on subtotal-plus-tax. There are quick 15/18/20/22/25% buttons.

**Other charges.** Delivery fees, resort fees, service charges, cleaning fees,
corkage — any number of them, each as a percentage or a flat amount, and each
split either evenly or in proportion to what people ordered. Discounts too.

**Categories.** Food & Restaurant, Groceries, Airbnb / Hotel, Transport, Flights,
Activities, Drinks, Utilities, Rent, Shopping, Household, Other — and you can
add your own. Every group gets a spend-by-category breakdown.

**Guests.** Not everyone splitting a bill needs a login. Add a guest by name to
include someone's partner or the friend who never signed up.

**Share links — let people pick their own items.** Generate a link for a bill
and send it round. Whoever opens it picks their name from the people on the
bill — or adds their own if they are not listed — ticks what they had, and sees
what they owe. No account, no sign-in. You watch the picks arrive live from the
bill editor, then close the link once everyone has answered.

**Settle up.** The app works out the *fewest* transfers that clear every debt
("Dana pays Jon $42.96") and lets you record payments as they happen. Export any
group to CSV.

**Groups.** A group is a trip, a household, or just a set of friends. Bills live
inside one. Groups can be archived when a trip is over.

### The money is exact

Every amount is stored and calculated in integer cents, percentages go through
`Decimal`, and every split uses the largest-remainder method — so what each
person owes always adds up to the bill total, to the cent. A $39.99 tab across
four people comes out $10.00 / $10.00 / $10.00 / $9.99, never four times $10.00.

There is a test that generates 400 random bills with every combination of
discount, tax, tip and fees and asserts not one penny goes missing.

## Admin

The first account is the admin, and can:

- **Approve or decline** account requests — nobody signs in unnotified.
- **Reset a password**, which issues a one-time password, signs that person out
  everywhere, and forces them to choose a new one at next sign-in.
- **Suspend** an account (reversible, signs them out immediately) or **delete**
  it outright.
- **Create accounts directly**, skipping the request step.
- **Promote other admins.** The app refuses to leave itself with zero admins.
- Close sign-ups entirely, set default currency/tax/tip, take a backup, and read
  an activity log of every administrative action.

**Deleting an account does not corrupt your history.** That person's share of past
bills is rewritten onto a guest named "Their Name (removed)" in each group they
took part in, so old totals and balances stay correct — those numbers are what
people actually owe each other. There is a checkbox to discard the history
instead if you really want to.

## Share links in detail

Open a saved bill and press **Create a share link**. You get a URL like
`http://your-pi:9100/s/xeM_RSNhNUx1IuOY81M8SMYC5VlwR2yj` to paste into the group
chat.

What the person opening it gets, in three taps:

1. **Which one are you?** — the people already on the bill, showing who has
   picked already. Or "I'm not on the list", where they type their name and are
   added as a guest.
2. **What did you have?** — the receipt, one line per item. Tapping an item
   claims it; an item claimed by several people is split between them and says
   who it is shared with.
3. **You owe $X** — their share of the items they picked plus their share of
   tax, tip and fees, and what everyone else owes.

Their choice of name is remembered in that browser, so coming back to the link
does not ask again.

Meanwhile the bill editor shows a live panel: who has picked, how many items
each, how many times the link has been opened. Toggle **Let people add their own
name** off to freeze the guest list, or **Close for changes** once everyone has
answered — the link still shows the bill, it just stops accepting edits.
**Replace** issues a new token and kills the old link; **Delete** removes it
entirely. Picks already made survive both.

The link is a bearer credential: anyone holding it can pick, and can pick *as*
anyone on the bill. That is unavoidable for a link you paste into a chat, so the
app is careful about the blast radius:

- A link exposes exactly one bill — its items, amounts and the display names of
  the people splitting it. No usernames, no emails, no other bills, no group
  balances, no admin surface. There is a test asserting the payload contains
  none of those.
- Every write is scoped to one person and to items on that one bill, so two
  people ticking at the same table cannot overwrite each other.
- Taking over a name that has already picked needs a confirmation.
- Tokens are 32 random URL-safe characters, and repeated bad guesses from one
  address get rate-limited.

**A note on editing a shared bill.** If you have the bill open in the editor
while people are picking, saving would overwrite their choices. Every bill
carries a revision counter, and a save that was based on an older revision is
refused with an offer to reload — so their picks cannot be silently lost. This
is why `bills.revision` exists rather than comparing timestamps: `updated_at`
has one-second resolution, and a pick landing in the same second you loaded the
page would have slipped straight through.

## Auto-update

Set your repo under **Admin → Updates** (`owner/name`). From then on the app
checks GitHub every hour. Leave auto-update off and it just tells you an update
is waiting; turn it on and it installs updates by itself.

The update is deliberately cautious, because the thing being updated is the
thing doing the updating:

1. **Refuses** if the working tree has local edits, so a file you hand-patched
   on the Pi is never silently overwritten. (There is a Force update if you
   want them gone.)
2. **Backs up the database** first.
3. `git fetch` and `git reset --hard` to the fetched commit.
4. `pip install` only when `requirements.txt` actually changed.
5. **Smoke-tests the new code in a separate process** (`python -c "import
   app.main"`). If the new version will not even import, it rolls straight back
   to the previous commit and stays up on the old version.
6. Restarts only then — by exiting, so systemd (`Restart=always`) brings it back
   on the new code. No `sudo` from inside the app.

Every attempt is logged with its result and the commits involved, visible in
Admin → Updates. Checking also shows you the commit messages you are about to
install.

This repo is public, so updating needs no credentials at all. If you ever make
it private, add a GitHub token under Admin → Updates for the API check (it is
write-only and never sent back to the browser), and give the Pi a deploy key or
credentials in its git remote so `git fetch` can still authenticate.

## How it is built

Python 3.10+, FastAPI, SQLite, and a vanilla-JS frontend. No build step, no
bundler, no CDN — the whole UI is plain ES modules served off the same origin,
which also means it works on a Pi with no internet access.

Dependencies are deliberately just `fastapi` and `uvicorn`. Password hashing is
stdlib `hashlib.scrypt`, the GitHub calls are stdlib `urllib`, so `pip install`
on a Pi compiles nothing and takes seconds.

```
app/
  main.py        entrypoint, static mounting, hourly update scheduler
  config.py      env/.env config; DB-backed settings live in the settings table
  db.py          schema, migrations, backups
  security.py    scrypt password hashing, DB-backed sessions
  splitter.py    the split engine - pure functions, no I/O
  ledger.py      DB <-> splitter glue; group balances and settle-up
  updater.py     GitHub check, apply, verify, roll back
  deps.py        auth dependencies (who is calling, are they allowed)
  routers/       auth, admin, groups, bills, share, system
static/          index.html, share.html, css/, js/ (ES modules, no build)
tests/           splitter, API, live-server, updater, share, migration tests
install.sh       the one-script installer
```

### Notes on the design

- **Money is integer cents everywhere.** Never a float.
- **A "party"** is anyone who can be on a bill, keyed `u:<id>` for an app user
  or `g:<id>` for a guest. That one string makes bills, balances and settlements
  uniform and made deleting-a-user-without-losing-history straightforward.
- **Bills are saved by full replace** (`PUT`), not a pile of nested PATCH
  endpoints. A bill is small, the editor holds the whole thing anyway, and a
  wholesale replace can never leave one half-updated.
- **The editor previews on the server.** The numbers you watch while typing come
  from the same function that computes the saved bill, so the preview cannot
  disagree with the result.
- Sessions live in the database, so an admin suspending or deleting an account
  kills its live sessions immediately.
- **Share-link writes are surgical**, not whole-bill replaces, precisely so
  several people claiming at once do not clobber one another. The owner's editor
  is the one place that replaces wholesale, and that is what the revision check
  protects.

## Tests

```bash
python tests/test_splitter.py       # the split engine (unit, incl. a 400-bill fuzz)
python tests/test_api.py            # the whole API against a temp database
python tests/test_live_server.py    # a real uvicorn server, real HTTP, concurrency
python tests/test_updater.py        # update, roll back, refuse-when-dirty
python tests/test_share.py          # share links, claiming, the lost-update guard
python tests/test_migrations.py     # upgrading an old database in place
```

Or all at once with `python -m pytest tests -q` if you have pytest.

The live-server tests exist for a reason: `TestClient` drives the app through a
single thread and happily passed a build where every request failed in
production, because FastAPI resumes a sync dependency's cleanup on a *different*
threadpool thread than the one that opened the SQLite connection.

`test_updater.py` builds a throwaway git repo, pushes a deliberately broken
commit to it, and asserts the app rolls back and still serves — so the rollback
path is exercised for real rather than hoped about.

## Configuration

`.env` in the install directory (the installer generates it):

| Variable | Default | Notes |
|---|---|---|
| `BILLSPLIT_PORT` | `9100` | 9000 is another project's |
| `BILLSPLIT_HOST` | `0.0.0.0` | all interfaces |
| `BILLSPLIT_SECRET_KEY` | generated | keep it |
| `BILLSPLIT_DATA_DIR` | `./data` | database and backups |
| `BILLSPLIT_COOKIE_SECURE` | `false` | set `true` behind https |
| `BILLSPLIT_SESSION_DAYS` | `30` | how long a sign-in lasts |
| `BILLSPLIT_SERVICE` | `bill-splitter` | systemd unit to restart |
| `BILLSPLIT_UPDATE_REPO` | — | default update source |

Everything else — currency, default tax and tip, whether sign-ups are open,
auto-update settings — is editable in the UI and stored in the database.

## Backups

`data/billsplit.db` is the whole application. Snapshots are taken automatically
before every update and before deleting a user or a group, into
`data/backups/` (last 15 kept), and an admin can take one on demand. To restore,
stop the service, copy a snapshot over `data/billsplit.db`, start it again.

Copy `data/` somewhere off the Pi now and then. SD cards die.

## Security

Sign-in is required for everything except the sign-in page itself. Passwords are
scrypt-hashed, sessions are opaque random tokens in HttpOnly SameSite=Lax
cookies, sign-in attempts are rate-limited per IP and username, and the app
sends a strict CSP with no external origins. All UI text is inserted as
`textContent`, so there is no HTML injection path.

This is designed for a home network. If you expose it to the internet, put it
behind a reverse proxy with TLS and set `BILLSPLIT_COOKIE_SECURE=true`.

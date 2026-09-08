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

## Updating

Either press **Update now** in Admin → Updates, or on the Pi:

```bash
cd ~/bill-splitter && ./install.sh
```

Re-running the installer fetches the latest code, installs any new
dependencies, migrates the database in place and restarts the service. It never
touches your data, and it refuses to overwrite files you have edited locally.

Auto-update is **off** by default: until you turn it on (Admin → Updates) or
press the button, the app stays on the version you installed. It still checks
hourly and tells you when something is waiting.

### If an update does not seem to have landed

```bash
cd ~/bill-splitter && ./install.sh --doctor
```

That prints which directory the service actually runs from, the commit and
version on disk, how many commits behind the remote you are, whether there are
local edits blocking the pull, and what the running app reports on
`/api/health`. Those five facts separate the cases that look identical from the
outside: a stale browser cache, an update that never fetched, a service started
from a different directory, and a service that never restarted.

`curl localhost:9100/api/health` on its own reports the version and commit that
are actually serving requests, which is the fastest way to tell a browser
caching problem from a code problem.

If the version on disk is current but the browser still looks old, hard-refresh
once (Ctrl+Shift+R). Versions before 1.2.1 served the frontend without a
`Cache-Control` header, so browsers were free to reuse the old JavaScript
without checking.

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
split either evenly or in proportion to what people ordered.

**Discounts, for everyone or for one person.** A group rate comes off the whole
bill; somebody's coupon or comped dish comes off *their* share alone, so the
rest of the table does not quietly pay for it. When it is aimed at one person a
percentage means a percentage of their share, and their tax and tip drop with
it. It is capped at their share, so nobody ends up owing less than nothing.

**Divide a line into portions.** One line for three beers or eight slices of
pizza: set how many portions it holds and each person takes however many they
had. The split follows the portions taken, so two beers and one beer is a 2:1
split of that line rather than a 50/50 one.

**Sub-items for modifications.** Hang extras off a line — add bacon, oat milk,
no onions, extra cheese. A sub-item is never claimed on its own: its cost
follows whoever took the parent, so ticking the burger picks up its bacon
automatically. That holds on the share page too.

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
   who it is shared with. A divided line gets a −/+ stepper instead ("2 of the
   4 glasses"), and a line's modifications are listed under it so you can see
   that ticking the burger includes its bacon.
3. **You owe $X** — their share of the items they picked plus their share of
   tax, tip and fees, and what everyone else owes.

Their choice of name is remembered in that browser, so coming back to the link
does not ask again.

**Which address the link uses.** By default it is built from whatever address
you are browsing from, which is right when that is the address your guests use
too. If you reach the app on a LAN address but your guests do not, set
**Admin → Settings → Public address** (e.g. `https://bills.example.com`) and
links use that instead. The editor says which one it is offering, and warns when
a link would only work on your own network.

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
- **A sub-item holds no claims of its own.** Its cost resolves to whoever
  claimed its parent, in the split engine. Making that a property of the
  calculation rather than something the UI mirrors means the two can never drift
  apart, and neither the editor nor the share page has to remember the rule.
- **Divided lines reuse the existing weighted split.** "Portions taken" is just
  the claim's weight, so three beers split 2:1 goes through exactly the same
  largest-remainder code as everything else, with no second money path to keep
  correct.
- **Rebuilding the editor form preserves scroll and moves focus deliberately.**
  Adding an item puts the cursor in its name field; flipping tax to a percentage
  puts it in the percentage box. A naive re-render throws you back to the top of
  the page, which is miserable on a phone at a restaurant table.

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
| `BILLSPLIT_TRUSTED_PROXIES` | `127.0.0.1,::1` | whose `X-Forwarded-For` to believe |
| `BILLSPLIT_UPDATE_REPO` | — | default update source |

Everything else — currency, default tax and tip, whether sign-ups are open,
auto-update settings — is editable in the UI and stored in the database.

## Backups

`data/billsplit.db` is the whole application. Snapshots are taken automatically
before every update and before deleting a user or a group, into
`data/backups/` (last 15 kept), and an admin can take one on demand. To restore,
stop the service, copy a snapshot over `data/billsplit.db`, start it again.

Copy `data/` somewhere off the Pi now and then. SD cards die.

## Behind Cloudflare Tunnel / Zero Trust

Two things bite here, and both look like bugs in the app.

**Cloudflare caches your JavaScript at the edge.** It caches `.js` and `.css`
by extension, so after a self-update the edge can keep handing out the previous
version. This survives a hard refresh *and* a private window, because the stale
copy is not in your browser at all. The app now sends `CDN-Cache-Control:
no-store` and `Cloudflare-CDN-Cache-Control: no-store` on every response, which
tells the edge not to store anything while still letting the browser do cheap
ETag revalidation.

Anything cached before you upgraded to 1.2.4 is still at the edge, so **purge it
once**: Cloudflare dashboard → your domain → Caching → Configuration → *Purge
Everything*. Belt and braces, add a Cache Rule for the hostname with *Bypass
cache* so it never depends on the origin headers being honoured.

The quickest way to tell an edge-cache problem from a code problem is to compare
the tunnel hostname against the Pi directly:

```bash
curl -s https://bills.example.com/api/health   # through Cloudflare
curl -s http://localhost:9100/api/health       # on the Pi
```

Different answers mean the edge is serving something stale, and no amount of
reinstalling will change it.

**Exposed to the internet, tighten two things.** Set a public address under
Admin → Settings so share links are built from your domain rather than whatever
address you happen to be browsing from, and turn off "let people request an
account" once everyone has one - otherwise strangers can queue up requests for
you to decline. Sign-in attempts are rate limited per IP and username, and the
app only believes `X-Forwarded-For` from `127.0.0.1` by default, so nothing that
can reach the port directly can spoof an IP to get around that.

**If `cloudflared` runs on a different machine** - which it does whenever the
tunnel's origin URL is a LAN address like `http://192.168.10.198:9100` rather
than `localhost` - that default is too narrow: every visitor then looks like
they are coming from the tunnel host, so per-IP limits apply to all of them at
once. Point it at the machine running `cloudflared`:

```
BILLSPLIT_TRUSTED_PROXIES=192.168.10.42
```

A subnet works too (`192.168.10.0/24`), at the cost of trusting anything on your
LAN to state its own client IP. To find the address, look at whose requests are
arriving:

```bash
sudo journalctl -u bill-splitter -n 200 | grep -oE '^INFO: +[0-9.]+' | sort -u
```

Restart after editing `.env` (`sudo systemctl restart bill-splitter`).

**If you put Cloudflare Access in front, it will block your share links.** If the whole hostname sits
behind an Access policy, anyone opening `/s/<token>` gets an Access login screen
and cannot get in unless they are in your policy - which defeats the point of a
link you send to friends. Add an Access application with a **Bypass** policy
(`Everyone`) covering the guest paths:

```
/s/*            the share page
/api/share/*    the endpoints it calls
/js/*  /css/*   the frontend it loads
/icon.svg  /manifest.webmanifest
```

Leave `/`, `/api/` and everything else behind Access as normal. The share token
is the credential for those paths, and they expose exactly one bill - see
[Share links in detail](#share-links-in-detail).

**Cookies.** Served over HTTPS through the tunnel you can tighten the session
cookie in `.env`:

```
BILLSPLIT_COOKIE_SECURE=true
```

One caveat: a `Secure` cookie is only sent over HTTPS, so this breaks signing in
via the plain-HTTP LAN address (`http://192.168.x.x:9100`). Set it only if you
always reach the app through the tunnel.

## Security

Sign-in is required for everything except the sign-in page itself. Passwords are
scrypt-hashed, sessions are opaque random tokens in HttpOnly SameSite=Lax
cookies, sign-in attempts are rate-limited per IP and username, and the app
sends a strict CSP with no external origins. All UI text is inserted as
`textContent`, so there is no HTML injection path.

This is designed for a home network. If you expose it to the internet, put it
behind a reverse proxy with TLS and set `BILLSPLIT_COOKIE_SECURE=true`.

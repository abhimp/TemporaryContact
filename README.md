# Temporary Contacts

A self-hosted **CardDAV account** whose contacts **expire automatically**.

Add the account on your iPhone and set it as the **Default Account**. Every new
contact you create (from anywhere on the phone) lands in this account, syncs to
your server, and is deleted after a retention period you control from a small
**web control panel**. Retention is a *floor*: a contact lives **at least** its
chosen duration.

No native app, no App Store, no Xcode. Just a Python service on your VPS behind
nginx/Apache, and a web page you can "Add to Home Screen".

## How it works

- **Radicale** (embedded) serves CardDAV at `/dav`; iOS syncs contacts as vCards.
- A **Flask** web panel at `/` lists contacts and lets you set/change each one's
  retention and delete on demand.
- A **background worker** assigns the default retention to new contacts and
  deletes expired ones; deletions sync back and disappear from the phone.
- Contacts (vCards) are stored by Radicale as files; only retention metadata
  lives in the app database (**SQLite or PostgreSQL**, your choice).

Everything is driven by a single **`config.yml`**.

## Install (on your VPS)

```sh
git clone <your-repo> /opt/temporarycontacts
cd /opt/temporarycontacts
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

cp config.example.yml config.yml      # then edit config.yml (see below)
mkdir -p data
# create your CardDAV + web login (bcrypt):
htpasswd -B -c data/users.htpasswd yourname
```

Run it:

```sh
python run.py -c config.yml
```

By default it listens on `127.0.0.1:5232` (HTTP) and expects a TLS reverse proxy.

## Configure `config.yml`

The knobs you'll usually touch:

| Setting | Meaning |
|---|---|
| `server_name` | Title shown in the web panel / home-screen app |
| `logo_path` | Path to a PNG/SVG logo (PNG recommended for the iOS home-screen icon) |
| `domain_name` | Your public domain |
| `network.reverse_proxy` | `true` = nginx/Apache terminate TLS (recommended) |
| `tls.cert` / `tls.key` | Only when `reverse_proxy: false` (app serves HTTPS directly) |
| `retention.default_days` | Default lifespan for new contacts (changeable in the UI) |
| `database.type` | `sqlite` or `postgresql` |

## TLS + reverse proxy

iOS requires the CardDAV account over **HTTPS**. Recommended: keep
`reverse_proxy: true` and let nginx/Apache handle TLS.

- nginx: see [`deploy/nginx.conf.example`](deploy/nginx.conf.example)
- Apache: see [`deploy/apache.conf.example`](deploy/apache.conf.example)
- Certificates: `certbot --nginx -d contacts.example.com` (or `--apache`)
- Run as a service: [`deploy/temporarycontacts.service`](deploy/temporarycontacts.service)

(Standalone TLS without a proxy: set `reverse_proxy: false` and `tls.cert`/`tls.key`;
the app then serves HTTPS via cheroot.)

## Add the account on your iPhone

1. **Settings → Contacts → Accounts → Add Account → Other → Add CardDAV Account.**
2. **Server:** `contacts.example.com` · **Username/Password:** your htpasswd login.
3. **Settings → Contacts → Default Account →** choose this account.
4. Create a contact — it appears in the web panel and expires per the default.
5. Open `https://contacts.example.com` in Safari → **Share → Add to Home Screen**
   for an app-like icon (uses your `logo_path`).

## PostgreSQL (optional)

Set in `config.yml`:

```yaml
database:
  type: postgresql
  host: localhost
  name: temporarycontacts
  user: temporarycontacts
  password: "..."
```

The `psycopg` driver is already in `requirements.txt`.

## Roadmap

- Server-side sync with **Google** and **Outlook** (interface stubbed in
  `temporarycontacts/sync/`).

## Maintenance

- One-off expiry pass (e.g. from cron instead of the built-in worker):
  `python run.py -c config.yml --cleanup`

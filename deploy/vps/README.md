# Running all your clients on one cheap VPS

One small server runs everything: a single Postgres database engine (holding a
**separate database per client**), one small copy of the app per client, and
Caddy, which gives every client automatic HTTPS. Adding a client is one command.

```
                        ┌──────────── your VPS (~$5/mo) ────────────┐
   client1.you.com ──►  │  Caddy ──► app-client1 ──►  coop_client1  │
   client2.you.com ──►  │        └─► app-client2 ──►  coop_client2  │  ◄─ one Postgres,
                        │                             (one DB each)  │     many databases
                        └────────────────────────────────────────────┘
```

Data stays fully separated — each client has its own database. All app copies
run the same code, so updates go out to everyone at once.

---

## First-time setup (about 20 minutes)

### 1. Get a server and a domain
- **VPS:** create an **Ubuntu 24.04** server (Hetzner, DigitalOcean, or Vultr).
  2 GB RAM is comfortable for a handful of clients. Note its **public IP**.
- **Domain:** buy one (e.g. `yourcoop.com`). You'll give each client a
  sub-address like `client1.yourcoop.com`.

### 2. Point the domains at the server
At your domain provider, add an **A record** for each client sub-address,
pointing at the server's IP:
```
client1.yourcoop.com   A   <server IP>
client2.yourcoop.com   A   <server IP>
```
(HTTPS won't be issued until DNS points here, so do this early.)

### 3. Install Docker on the server
SSH into the server, then:
```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/keshdel/oou_cooperative_system/main/deploy/vps/install-docker.sh)"
```
Or clone the repo first and run `sudo deploy/vps/install-docker.sh`.

### 4. Get the code
```bash
git clone https://github.com/keshdel/oou_cooperative_system.git
cd oou_cooperative_system/deploy/vps
```

### 5. Add each client — one command each
```bash
./add-client.sh client1 client1.yourcoop.com
./add-client.sh client2 client2.yourcoop.com
```
That's it. Each command:
- generates that client's secrets and admin password,
- creates their database,
- builds/starts their app copy,
- adds their HTTPS domain (Caddy fetches the certificate automatically).

Open `https://client1.yourcoop.com`, log in as **admin** (password is printed at
the end and saved in `clients/client1.env`), change the password, then import
their members under **Data Migration**.

---

## Everyday tasks

**Add another client later**
```bash
./add-client.sh client3 client3.yourcoop.com
```

**Create the CoopMS owner/HQ back office**
```bash
./add-client.sh hq hq.cooperativems.com
echo hq > .landing-api-client
python3 generate.py
docker compose up -d --build
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```
Use the HQ portal for CoopMS-owned operations such as website leads, demo
requests, prospect follow-up, onboarding setup, and future CRM sync. Client
portals such as `ooucoop.cooperativems.com` and `smtcoop.cooperativems.com`
remain client-owned and do not show the marketing lead inbox unless their env
file explicitly sets `MARKETING_HQ=1`.

**Configure outgoing email for any tenant**
Edit that tenant's env file, for example:
```bash
nano clients/hq.env
```
Minimum Brevo SMTP setup:
```env
MAIL_ENABLED=1
MAIL_FROM=CoopMS <verified-sender@example.com>
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=<brevo-smtp-login>
SMTP_PASS=<brevo-smtp-password>
SMTP_USE_TLS=1
SMTP_USE_SSL=0
```
For HQ lead alerts, also set:
```env
MARKETING_LEAD_NOTIFY_EMAIL=you@example.com
MARKETING_HQ_BASE_URL=https://hq.cooperativems.com
MARKETING_SITE_URL=https://www.cooperativems.com
```
Then restart the affected app:
```bash
docker compose up -d --build app-hq
```
Use `app-ooucoop` or `app-smtcoop` for client tenant email changes. Each
tenant has its own sender credentials, so a client's member emails do not mix
with CoopMS HQ sales emails.

**Update all clients to the latest code**
```bash
git pull
docker compose up -d --build
```
The app updates its own database structure on start, safely, without losing data.

**See what's running**
```bash
docker compose ps
```

**Turn on nightly backups** (keeps 14 days, one file per client)
```bash
crontab -e
# add this line (adjust the path):
0 2 * * *  cd /root/oou_cooperative_system/deploy/vps && ./backup.sh >> backups/backup.log 2>&1
```

**Restore one client from a backup**
```bash
gunzip -c backups/coop_client1-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose exec -T postgres psql -U postgres -d coop_client1
```

If `BACKUP_PASSPHRASE` is set the files end in `.sql.gz.enc` and have to be
decrypted first. It needs the same passphrase — which is exactly why that
passphrase belongs somewhere other than this server:
```bash
BACKUP_PASSPHRASE='...' openssl enc -d -aes-256-cbc -pbkdf2 \
  -pass env:BACKUP_PASSPHRASE \
  -in backups/coop_client1-YYYYMMDD-HHMMSS.sql.gz.enc | \
  gunzip -c | docker compose exec -T postgres psql -U postgres -d coop_client1
```

**Remove a client**
```bash
./remove-client.sh client1            # stop the app, keep the data
./remove-client.sh client1 --drop-db  # also delete the database (permanent)
```

---

## Good to know
- **Secrets never leave the server.** `deploy/vps/.env` and everything in
  `clients/` are gitignored. Keep a copy of each client's admin password in your
  password manager.
- **Email:** set `MAIL_ENABLED=1` plus Brevo API, SMTP, or Resend in the
  client's `clients/<name>.env`, then restart that app container. Brevo SMTP is
  the easiest interim option before you have a verified sending domain.
- **Payments:** put each client's own Paystack keys in their `clients/<name>.env`.
- **Sizing:** if the server gets busy, resize it in the provider dashboard — no
  reinstall needed. One 2 GB box handles many small coops.
- **Postgres is private:** it isn't exposed to the internet; only the app
  containers reach it over Docker's internal network.

## Per-client database logins (security: F-01)

Every client app used to connect as the shared `postgres` **superuser**, so a break-in
to one client exposed all of them. Each client now gets its own login `coop_<name>` that
owns only `coop_<name>` and cannot connect to any other database.

New clients get this automatically (`add-client.sh`). **Existing clients must be migrated
once, one at a time** - do the smallest/least critical first and check it before the next:

```bash
cd ~/oou_cooperative_system/deploy/vps
git pull
bash harden-db-roles.sh smtcoop        # takes a backup, creates the role, switches the app, restarts it
docker compose logs --tail 30 app-smtcoop   # expect a normal start, no "permission denied"
python3 generate.py                    # lists any client still marked "!! still on the shared postgres SUPERUSER"
```

Roll back one client: delete the `DATABASE_URL=` line from `clients/<name>.env`, then
`python3 generate.py && docker compose up -d app-<name>`. When every client is migrated, rotate
`POSTGRES_PASSWORD` in `deploy/vps/.env` (it is now used only by backups and admin scripts).
Re-running `harden-db-roles.sh <name>` rotates that client's password.

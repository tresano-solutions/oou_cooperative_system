# F-02 — Rotate exposed credentials and make the repository private

Finding: the GitHub repository `tresano-solutions/oou_cooperative_system` is **public** and its
history contains credentials. Anything in a public repository must be treated as already copied.
**Rotating is what makes you safe. Making the repo private and cleaning history only stops new copies.**

This file contains no secret values on purpose.

## 0. What was found (values deliberately not shown)

| Where | What | Action |
|---|---|---|
| `Postgres Database ur.txt` (tracked, on `main` and in history) | A hosted Postgres connection string **with a password** (Render, database name starting `coopdb_`) | Rotate or delete that database (step 1) |
| `docker-compose.yml` (root, tracked and in history) | A default database password used by the *legacy* compose file | Make sure that compose file was never run on the server (step 2) |
| `migrate_sqlite_to_postgres.py`, `test_pg.py` (tracked and in history) | A hard-coded database password | Change it wherever it was ever used, and never reuse it (step 3) |
| `docs/mobile-app.md`, `deploy/vps/landing/app/README.txt` (history) | The server's public IP address | Not a secret; just make sure the firewall and SSH are tight (step 6) |
| 10 branches on GitHub (`claude/*`, `codex/*`, `phase*`, …) | Same history | Covered by step 7 |

No other secret-looking values were found in tracked files or history (`scripts/scan_secrets.py --history`).
It cannot catch every format, so step 8 runs a second scanner.

## 1. Rotate the hosted database (do this first, today)

1. Sign in to the hosting dashboard that owns that database (Render).
2. If the database is **no longer used**: delete it (take a dump first if it holds anything you want).
3. If it **is still used**: change its password (or create a new database user), update the app that
   uses it, then confirm the old connection string no longer works.
4. While there, check its "access logs / connections" page for connections you do not recognise.

## 2. Confirm the legacy compose file never ran on your server

On the droplet:
```bash
docker ps -a --format '{{.Names}}' | grep -i 'oou-coop' || echo "none - good"
ss -tlnp | grep -E ':(5432|6379|9200|5601|5555|3000|9000|9090)\b' || echo "no exposed admin ports - good"
```
Both lines should say "good". If anything shows up, stop it (`docker compose down` in that folder),
change every password it used, and tell me - that is a bigger incident.

## 3. The reused password

Treat the password string as burned. Anywhere you (or anyone on the team) used it - local Postgres,
other services, other sites - change it now, and use a different one in each place.

## 4. Make the repository private (safe order)

The server pulls code from GitHub, so give it its own read-only key **before** you flip the switch.

**4a. On the droplet** (as the user that runs `git pull`):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/coopms_deploy -N "" -C "coopms-droplet-deploy"
cat ~/.ssh/coopms_deploy.pub          # copy this line
```
**4b. On GitHub** → repository → *Settings* → *Deploy keys* → *Add deploy key*: paste it, title
"droplet (read-only)", leave **"Allow write access" unticked**.

**4c. Back on the droplet:**
```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/coopms_deploy
  IdentitiesOnly yes
EOF
cd ~/oou_cooperative_system
git remote set-url origin git@github.com:tresano-solutions/oou_cooperative_system.git
git pull --ff-only                    # must succeed before you go private
```
**4d. Flip it:** GitHub → repository → *Settings* → *General* → scroll to **Danger Zone** →
*Change repository visibility* → **Make private** → type the repository name to confirm.

**4e. Check:**
```bash
gh repo view tresano-solutions/oou_cooperative_system --json visibility   # expect "PRIVATE"
cd ~/oou_cooperative_system && git pull --ff-only                          # still works on the droplet
```
Things that stop working once private (fix if you use them): the `curl … raw.githubusercontent.com/…/install-docker.sh | sudo bash`
line in `deploy/vps/README.md` (copy the script to the server instead), any CI, Expo/EAS or hosting
integrations that read the repo anonymously (re-authorise them), and any link you shared to files in the repo.
The repo currently has 0 forks and 0 stars, so nobody is left holding a public fork.

## 5. Turn on GitHub's protections

*Settings* → *Code security*:
- **Secret scanning** and **Push protection** (stops a future password from being pushed).
  On a private repo these may need GitHub Advanced Security on your plan; if unavailable, use step 8's scanner as a pre-commit hook.
- **Dependabot alerts** and **security updates**.

*Settings* → *Branches* → add a rule for `main`: require a pull request, require 1 approval (or at least
"require pull request" if you are the only person), block force pushes.
Your **GitHub account and any organisation members: enable two-factor authentication.**
*Settings* → *Collaborators*: remove anyone who should not have access.

## 6. Server access (public IP is in the history)

- SSH: key-only, `PermitRootLogin prohibit-password` (or a normal user + sudo), `PasswordAuthentication no`.
- Cloud firewall (DigitalOcean → Networking → Firewalls): inbound only 22 (ideally from your IP), 80, 443.
- DigitalOcean, Namecheap, Resend/Brevo and Paystack accounts: 2FA on.

## 7. Clean the history (hygiene — only after steps 1–3)

Once the credentials are dead, the history is just clutter, but remove it so scanners stop flagging it.
This **rewrites history**: everyone must re-clone afterwards, and open pull requests will break.
```bash
pip install git-filter-repo
git clone --mirror git@github.com:tresano-solutions/oou_cooperative_system.git coopms-clean.git
cd coopms-clean.git
git filter-repo --invert-paths --path "Postgres Database ur.txt" --path docker-compose.yml --path nginx.conf
printf 'PASSWORD_VALUE_1==>REMOVED\nPASSWORD_VALUE_2==>REMOVED\n' > ../replace.txt   # type the real strings here, locally only
git filter-repo --replace-text ../replace.txt
git push --force --mirror origin
```
Then delete stale branches you no longer need (`claude/*`, `codex/*`, `phase*`) on GitHub, and re-clone
on the droplet and on your PC. GitHub keeps old commits reachable by SHA for a while; if that matters,
ask GitHub Support to run garbage collection and purge cached views.

## 8. Verify

```bash
python scripts/scan_secrets.py --history          # expect: no findings
pip install gitleaks 2>/dev/null || true; gitleaks detect --source . --log-opts="--all"   # second opinion
gh repo view tresano-solutions/oou_cooperative_system --json visibility
```
And on the droplet, `docker compose logs --since 24h | grep -i "authentication failed"` after the rotation
to be sure nothing still depends on an old password.

## 9. About my own security branches

The local branches `security/*` contain the audit report. An earlier version of it quoted two of the
exposed passwords. I have removed them in the latest commit, but they remain in the **earlier local
commits**. Therefore:
- Do **not** push the `security/*` branches as they are.
- Instead, merge them with **"Squash and merge"** in pull requests (so only the clean final text reaches `main`),
  or let me create one fresh squashed branch from the final state. Both avoid publishing those older commits.

## Order of work (about 1–2 hours)

1 → 2 → 3 (rotate / confirm; 30 min) → 4a–4c (deploy key; 10 min) → 4d–4e (private; 5 min) → 5 → 6 → later 7, 8.

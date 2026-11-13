# DownloadBot: instructions for agents

## Working in this repository

- Read this file before changing or deploying the service. Operational details:
  [docs/VDS_DEPLOYMENT.md](docs/VDS_DEPLOYMENT.md). Keep those instructions current.
- Reuse the current checkout and its `main` branch when the user's task concerns
  this deployed project. Preserve unrelated user changes. Commit the completed work.
- Run Ruff and the relevant tests. For changes across downloader/API/bot, run the
  full suite with the dedicated PostgreSQL test database (name ends in `_test`).
  `.runtime/db.env` contains the local test DSN; never print it. Real FFmpeg tests
  use `DOWNLOADBOT_TEST_FFMPEG`, or FFmpeg from PATH.
- Never print `.env`, deployment env files, tokens, cookies, private keys or raw
  upstream diagnostics. Test failures may contain DSNs: capture and redact output.
- Preserve the downloader's URL validation, public-IP checks, disabled proxies,
  local-only FFmpeg protocols, process timeout/cancellation and disk/file limits.
- Telegram accepts private chats from its explicit allowlist only. Its API client
  has no admin rights or automatic access to technical accounts.

## Production access

This project has its **own** OVH VPS. Do not deploy to the older RemoteDrive VPS.

- Host: `162.19.255.134` (`vps-dd9e88e4.vps.ovh.net`), SSH user `ubuntu`, port 22.
- SSH key: `%USERPROFILE%/.ssh/downloadbot-vds`.
- Pinned hosts file: `%USERPROFILE%/.ssh/downloadbot_known_hosts`.
- Expected ED25519 SHA256 fingerprint:
  `kw4OlJ8G5XExUxZ6EkPlyurX3XqJE1g13P8eQlq/Yic`.
- Always use `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`,
  that known-hosts file, `ConnectTimeout=15`, `ServerAliveInterval=30`.
- Use key authentication and `sudo -n`. Password SSH is disabled. Do not expose
  or replace the owner's recovery credentials during an ordinary update.
- API listens on VPS `127.0.0.1:8000`; PostgreSQL has no public port. Keep it so.
  A local SSH tunnel may expose API at `127.0.0.1:18000`.

PowerShell SSH options (use the same options for SCP):

```powershell
$sshOptions = @('-i', "$env:USERPROFILE/.ssh/downloadbot-vds",
  '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
  '-o', 'StrictHostKeyChecking=yes',
  '-o', "UserKnownHostsFile=$env:USERPROFILE/.ssh/downloadbot_known_hosts",
  '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30')
ssh @sshOptions ubuntu@162.19.255.134 'sudo -n docker ps'
```

## Deployment procedure

1. Inspect Git status, current `/etc/downloadbot/release.json`, container health
   and available disk. Finish implementation and tests before deployment.
2. On the VPS run `sudo -n systemctl start downloadbot-backup.service`; verify
   its successful exit. Preserve the previous source archive and image IDs for
   rollback. Never run `docker compose down -v`, prune volumes, or replace the DB.
3. Prepare an archive of **tracked, committed** files only. Include a manifest of
   relative filenames, SHA-256 digests and the exact commit SHA. Exclude `.git`,
   `.env`, `.runtime`, `.venv`, downloads, cookies and all local secrets. Transfer
   the archive with SCP. Reject absolute paths, traversal, symlinks and special
   files before extraction; extract only under `/srv/downloadbot` and verify hashes.
4. Preserve `/srv/downloadbot/.env` → `/etc/downloadbot/deployment.env` and both
   root-only env files (mode 0600). Never regenerate passwords, the Fernet key,
   bot token, allowlist or client keys as part of a code update.
5. From `/srv/downloadbot`, use **all three** Compose files for this installation:

   ```sh
   docker compose --env-file /etc/downloadbot/deployment.env \
     -f compose.yaml -f deploy/compose.vds.yaml -f deploy/compose.telegram.yaml \
     config --quiet
   docker compose --env-file /etc/downloadbot/deployment.env \
     -f compose.yaml -f deploy/compose.vds.yaml -f deploy/compose.telegram.yaml \
     build api worker telegram
   docker compose --env-file /etc/downloadbot/deployment.env \
     -f compose.yaml -f deploy/compose.vds.yaml -f deploy/compose.telegram.yaml \
     up -d --wait api worker telegram
   ```

   Run as root using `sudo -n`; do not use `config` without `--quiet`, since its
   expanded output contains credentials. Additive schema initialization happens
   at startup; plan compatibility and backup restoration before destructive migrations.
6. Verify `/readyz`, DB/API/worker health, Telegram container stability, enabled
   systemd units and backup timer. Check the source hashes **inside the running
   images**, not only the host checkout. Record commit, hashes and verification
   time in `/etc/downloadbot/release.json` (0600).
7. Submit real supported public URLs through the authenticated API. Check file
   bytes and type, not only `succeeded`. For Telegram delivery changes verify the
   real response type (`Message.video`, `photo`, or media group) in the owner's
   already authorized chat; never message other chats. Do not run a second
   `getUpdates` poller alongside the bot. Record the results in `docs/` without secrets.
8. If verification fails, restore the previous source/images and restart the
   same services with the same volumes and env. Restore the database only when
   necessary and after checking the backup against a separate scratch database.
9. Publishing to GitHub is separate from deployment. Push only when the session
   authorizes public publication. If approval review rejects a push, do not bypass
   it through another tool: finish local/deployed work, explain the rejection and
   obtain the missing approval for the concrete commit.

## Windows/SSH pitfalls

- Prefer remote Python orchestration. PowerShell pipes shell scripts with CRLF;
  if sending a shell script, normalize CRLF before executing it.
- `docker compose exec -T` can consume the remaining stdin of a piped shell
  script. Give commands that do not need stdin `</dev/null`, upload the script
  first, or orchestrate subprocesses from remote Python.
- Start background Windows helpers with `-WindowStyle Hidden`. Keep private
  temporary tooling in the ignored `.runtime/` directory.

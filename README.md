# telegram_sampler

A Telegram bot that pings you at random times and logs your replies. It runs as a Docker container on a small cloud server and stores its data in a SQLite database.

This README covers installation on a free Oracle Cloud server. The steps work for any Linux VPS, and where Ubuntu and Oracle Linux differ, both are shown.

## What you'll need

- A Telegram account
- An Oracle Cloud account (the Always Free tier works; it requires a credit card for verification)
- A Mac or Linux computer with a terminal

## Quick reference: values used throughout

| Placeholder | Meaning |
|---|---|
| `<SSH_USER>` | `ubuntu` on Ubuntu images, `opc` on Oracle Linux images |
| `<SERVER_IP>` | The public IPv4 address of your instance |
| `~/.ssh/oracle.key` | Where you'll store the private SSH key on your computer |

## 1. Create the Telegram bot

1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts. At the end it gives you a **bot token**. Treat it like a password.
2. Message **@userinfobot**. It replies with your **numeric ID**. This is *your* Telegram user ID, and because the bot talks to you in a private chat, it is also your chat ID.

Keep both values handy. In step 6 you'll put them in a `.env` file as `BOT_TOKEN` and `OWNER_CHAT_ID`.

## 2. Create the Oracle Cloud account

1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/).
2. Choose your **home region** carefully, because it can't be changed later. Pick one near you (for example, US East Ashburn). The region doesn't matter for a polling bot, but it keeps timestamps simple.

## 3. Create the server

Go to **Compute → Instances → Create instance**.

- **Image:** choose **Canonical Ubuntu 24.04**. This is strongly recommended. If you pick Oracle Linux (the default) you'll need `dnf` instead of `apt`, and the SSH user is `opc` instead of `ubuntu`. This README gives both sets of commands, but Ubuntu is the simpler path.
- **Shape**, either of:
  - `VM.Standard.E2.1.Micro` (AMD, 1 GB RAM). Enough for a polling bot and usually available.
  - `VM.Standard.A1.Flex` (ARM, for example 1 OCPU and 6 GB RAM). More powerful, but often fails with "Out of capacity". If so, retry later or use the Micro.
- **Networking:** keep the defaults and make sure **Assign a public IPv4 address** is on. The default rules allow SSH (port 22) only, which is all a polling bot needs.
- **SSH keys:** either upload your own public key or let Oracle generate a pair. If Oracle generates it, **download the private key** (a `.key` file) before clicking Create. You can't get it again. Use key login only, never a password.

Create the instance, then copy its **public IP** from the instance page.

> **If the instance has no public IPv4 address:** this happens when the instance lands in a private subnet or the "assign public IP" box was off. Two fixes:
> - Open the instance → **Attached VNICs** → the VNIC → **IPv4 Addresses** → edit, and assign an ephemeral public IP. This only works if the VNIC's subnet is a *public* subnet.
> - Or create the network first (**Networking → Virtual Cloud Networks → Start VCN Wizard → Create VCN with Internet Connectivity**), then choose that VCN and its public subnet when creating the instance.

## 4. Set up your SSH key (on your computer)

Oracle gave you a private key file (for example `ssh-key-2026-11-02.key` in your Downloads folder). SSH refuses to use a private key that other users can read, so move it somewhere sensible and lock down the permissions:

```bash
mkdir -p ~/.ssh
mv ~/Downloads/ssh-key-*.key ~/.ssh/oracle.key
chmod 600 ~/.ssh/oracle.key
```

If you uploaded your own public key instead, SSH will find your default key automatically and you can drop the `-i ~/.ssh/oracle.key` part from the commands below.

## 5. Connect and update the server

```bash
ssh -i ~/.ssh/oracle.key <SSH_USER>@<SERVER_IP>
```

Remember: `ubuntu` for Ubuntu, `opc` for Oracle Linux. If you get "Permission denied (publickey)", you're probably using the wrong user name or the wrong key file.

Once connected, update the system:

**Ubuntu:**

```bash
sudo apt update && sudo apt upgrade -y
```

**Oracle Linux:**

```bash
sudo dnf upgrade -y
```

## 6. Install Docker

Run these one at a time.

**Ubuntu:**

```bash
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
```

**Oracle Linux:**

```bash
sudo dnf install -y dnf-utils
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker opc
```

The `docker-ce` install takes a long time on Oracle Linux. That's normal.

Then run `exit` and SSH back in so the group change takes effect. After that, `docker ps` should work without `sudo`.

## 7. Get the code onto the server

```bash
git clone https://github.com/ljohnston931/telegram_sampler.git
cd telegram_sampler
```

## 8. Configure

Copy the example environment file and fill it in:

```bash
cp .env.example .env
nano .env
```

Set the two values from step 1:

```
BOT_TOKEN=<token from @BotFather>
OWNER_CHAT_ID=<numeric ID from @userinfobot>
```

Save with Ctrl+O, Enter, then exit with Ctrl+X. The `.env` file holds secrets and is not committed to git.

The other settings in `.env.example` are optional. `PINGS_MEAN`, `PINGS_MIN` and `PINGS_MAX` are only starting values: once you use `/mean`, `/min` or `/max` in Telegram, the stored values take over (see [Behavior](#behavior)).

## 9. Build and run

```bash
docker build -t telegram_sampler .
sudo mkdir -p /opt/sampler-data
sudo chown $USER:$USER /opt/sampler-data
docker run -d --name telegram_sampler --restart unless-stopped \
  --env-file .env -v /opt/sampler-data:/data:Z telegram_sampler
```

- `--restart unless-stopped` brings the bot back after a crash or reboot.
- The `:Z` on the volume handles SELinux, which Oracle Linux has enabled. Without it you can get "permission denied" errors writing the database. On Ubuntu it is harmless.

## 10. Verify

1. Run `docker logs -f telegram_sampler` and look for the `plan <date>` line. Press Ctrl+C to stop watching.
2. Send `/status` and `/db` to your bot in Telegram.
3. Wait for the first ping.

## 11. Backups

A backup that lives only on the server isn't a real backup, so set up a nightly copy and pull it to your own computer. (The bot's `/export` command also works anytime.)

**On the server**, install SQLite and cron:

*Ubuntu:*

```bash
sudo apt install -y sqlite3 cron
sudo systemctl enable --now cron
```

*Oracle Linux:*

```bash
sudo dnf install -y sqlite cronie
sudo systemctl enable --now crond
```

Open the crontab with `crontab -e` and add this line (runs daily at 4:00 AM server time):

```
0 4 * * * sqlite3 /opt/sampler-data/sampler.db ".backup '/opt/sampler-data/backup.db'"
```

**On your computer**, pull the backup:

```bash
mkdir -p ~/backups
rsync -a -e "ssh -i ~/.ssh/oracle.key" <SSH_USER>@<SERVER_IP>:/opt/sampler-data/backup.db ~/backups/
```

Run this by hand every so often, or automate it with a `launchd` (macOS) or `cron` entry on your computer.

## 12. Updating later

On the server:

```bash
cd ~/telegram_sampler
git pull
docker build -t telegram_sampler .
docker rm -f telegram_sampler
docker run -d --name telegram_sampler --restart unless-stopped \
  --env-file .env -v /opt/sampler-data:/data:Z telegram_sampler
```

The data in `/opt/sampler-data` persists across updates, and the database is upgraded automatically on startup.

## Keeping the free instance alive

Oracle can reclaim idle Always Free instances. Upgrading the account to **Pay As You Go** (still $0 within Always Free limits) and setting a **$1 budget alert** avoids that.

## Bot commands

Send `/help` to the bot for the full list with accepted values. The main ones:

- `/status`: show the mean, min, max, window, and pending and unanswered pings
- `/mean [X]`: show or set the Poisson mean pings per day (0.1 to 50, decimals ok)
- `/min [N]`: show or set the fewest pings a day can have (0 to 100, or `none`)
- `/max [N]`: show or set the most pings a day can have (1 to 100, or `none`)
- `/pause 4d | 12h | until nov 15` and `/resume`: stop and restart pings
- `/db [N]`: show database info and the last N pings and entries
- `/export`: send you a copy of the database

`/mean`, `/min` and `/max` take effect from the next day's plan.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied (publickey)` | Check the user (`ubuntu` vs `opc`) and that you pass `-i ~/.ssh/oracle.key`. |
| `WARNING: UNPROTECTED PRIVATE KEY FILE` | Run `chmod 600 ~/.ssh/oracle.key`. |
| `docker: permission denied` | You skipped the `exit` and re-login after `usermod -aG docker`. |
| Database "permission denied" errors | Make sure the `:Z` flag is on the volume mount and `/opt/sampler-data` is owned by your user. |
| `Out of capacity` creating an A1 instance | Retry later, or use the `E2.1.Micro` shape. |

## Development and tests

You don't need any of this to run the bot on the server. It's for working on the code on your own computer.

### Requirements files

- `requirements.txt` lists what the bot needs to run. This is what the Docker image installs.
- `requirements-dev.txt` includes everything in `requirements.txt` (via `-r requirements.txt`) and adds `pytest`.

### Python version

You need Python 3.9 or newer (the bot uses `zoneinfo`, which was added in 3.9). Check with:

```bash
python --version
```

If you manage Python versions with pyenv, you can pin the project to one with `pyenv local 3.9.1` (or a newer version).

### One-time setup

From the project folder, create a virtual environment and install the dev requirements into it:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run `source .venv/bin/activate` again in each new terminal before running tests. You'll know it's active when your prompt starts with `(.venv)`. Run `deactivate` to leave it.

On Windows, activate with `.venv\Scripts\activate` instead, and also run `python -m pip install tzdata`, because Windows has no built-in timezone database.

### Running the tests

```bash
python -m pytest -q
```

Useful variations:

```bash
python -m pytest                              # verbose-ish output
python -m pytest test_bot.py                  # one file
python -m pytest -k "parse_at"                # only tests whose name matches
python -m pytest test_bot.py::TestTick        # one test class
python -m pytest -x                           # stop at the first failure
```

Use `python -m pytest` rather than plain `pytest`. With pyenv or several Pythons installed, plain `pytest` can run under a different Python than `python` does, which causes confusing "module not found" errors.

The tests don't need a `.env` file, a Telegram connection, or your real database. Each test uses a temporary SQLite file, and Telegram messages are faked.

### What's tested

- `test_sampling.py`: the ping-count and timing logic in `sampling.py`.
- `test_bot.py`: everything in `bot.py` except the Telegram startup code in `main()`: time parsing, scheduling, entry handling, the commands, and the database migration.

## Behavior

**Schedule.** Each day has a sampling window (default 08:00 to 02:00 local time, so it runs past midnight). The day's plan is made once, at the start of that day's date, and survives restarts.

**How many pings.** Each day's count is drawn from a Poisson distribution with the mean you set, then the pings are placed uniformly at random in the window. With no min or max this is a true Poisson process: the pings you've had so far tell you nothing about how many are still to come, and a day can have zero pings.

**Min and max.** If you set a min and/or max, the count is drawn from the Poisson distribution restricted to that range, which is the same as redrawing any day that falls outside it. Set both to `none` to let it run free. Bounds reintroduce a small amount of predictability (with a max of 6, you know a day is done after the sixth ping), so wider bounds leak less. Pings can land close together; there is no minimum gap.

**Partial first day.** If the bot starts partway through a window, the mean is scaled by the fraction of the window left, and min and max are not applied to that partial day.

**Replying.** Just type to answer the oldest unanswered ping, or reply to a specific ping. The entry is stored with the ping's time, not the time you replied. `skip` skips a ping. `now <text>`, `@3:30pm <text>` and `-1h30m <text>` log entries that aren't tied to a ping. A ping that is more than `LATE_GRACE_MIN` minutes overdue (for example, the bot was down) is marked missed instead of being sent late.

**What's recorded.** Each day's row in `day_plans` stores the number of pings and the distribution it was drawn from (mean, min, max), so you can account for truncation when analyzing the data. Changes to settings are logged in `settings_log`. All times are stored in UTC.
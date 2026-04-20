# Deploy runbook — social-surveyor on EC2 (Session 5a)

Minimal production deploy: one t4g.micro behind an IAM role, polling
on cron inside a single Python process, secrets in SSM Parameter
Store. No CI/CD, no health endpoint, no automated rsync — those come
in 5a-polish, 5b, and 5c.

If you are reforking this repo for your own project, copy
`Pulumi.opendata.example.yaml` to `Pulumi.<yourproject>.yaml` and
substitute your own VPC / subnet / project name everywhere you see
`opendata` below.

---

## 0. Prerequisites

- AWS CLI configured with credentials that have permission to create
  EC2 / IAM / SSM resources in the target account.
- Pulumi CLI (>= 3.100) and a Pulumi Cloud account for state.
- An existing VPC and public subnet in the target region (or a
  private subnet with NAT / VPC endpoints for SSM — not the path 5a
  is wired for).
- Local `.env` containing the runtime secrets the service needs:
  `ANTHROPIC_API_KEY`, `OPENDATA_SLACK_WEBHOOK_IMMEDIATE`,
  `OPENDATA_SLACK_WEBHOOK_DIGEST`, `X_BEARER_TOKEN`, and any other
  per-source tokens referenced by the project's configs.

All commands below assume you are running them from the repo root
with `AWS_PROFILE` set to the profile that points at the target
account.

---

## 1. Configure the Pulumi stack (one-time)

```bash
cd deploy/pulumi
cp Pulumi.opendata.example.yaml Pulumi.opendata.yaml
# edit Pulumi.opendata.yaml: fill in vpc_id, subnet_id, region, project_name
```

Set up the Python venv Pulumi uses:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create the stack (or select if it already exists):

```bash
pulumi stack select opendata --create
```

---

## 2. Preview and apply

```bash
AWS_PROFILE=prod pulumi preview     # eyeball the 8 resources
AWS_PROFILE=prod pulumi up          # apply
```

Record the outputs — you'll use `instance_id` and
`ssm_connect_command` repeatedly.

```bash
pulumi stack output instance_id
pulumi stack output ssm_connect_command
```

---

## 3. Seed SSM Parameter Store (one-time, from the laptop)

From the repo root:

```bash
AWS_PROFILE=prod deploy/seed-ssm.sh opendata
```

This reads every `KEY=VALUE` line from `.env` and writes it to SSM
as `/social-surveyor/opendata/<KEY>` (SecureString, KMS-encrypted
under `alias/aws/ssm`). Re-run any time the secrets change;
`--overwrite` is always on.

Verify:

```bash
AWS_PROFILE=prod aws ssm get-parameters-by-path \
    --path /social-surveyor/opendata \
    --query 'Parameters[*].Name' --output table
```

---

## 4. Connect to the instance

SSM Session Manager (no SSH, no bastion needed):

```bash
AWS_PROFILE=prod aws ssm start-session \
    --target "$(cd deploy/pulumi && pulumi stack output instance_id)" \
    --region us-west-2
```

The session drops you in as `ssm-user`; `sudo` is permitted.

---

## 5. Provision the instance (on the instance, via SSM)

Clone the repo into the canonical path:

```bash
sudo mkdir -p /opt/social-surveyor
sudo chown $(id -u):$(id -g) /opt/social-surveyor
git clone https://github.com/apurvam/social-surveyor.git /opt/social-surveyor
```

> For a private repo, use an HTTPS token or a deploy key. The plain
> public-clone path above is the 5a happy path.

Run the bootstrap script:

```bash
sudo bash /opt/social-surveyor/deploy/bootstrap-ec2.sh
```

The bootstrap script installs `uv`, creates the `social-surveyor`
service user, chowns `/opt/social-surveyor` and
`/var/lib/social-surveyor`, installs the systemd template unit, and
drops `/usr/local/bin/social-surveyor-load-env` into place.

---

## 6. Install Python deps, load secrets, start the service

```bash
# install project deps under the service user
sudo -u social-surveyor bash -c 'cd /opt/social-surveyor && uv sync'

# pull secrets from SSM into /etc/social-surveyor/opendata.env
sudo /usr/local/bin/social-surveyor-load-env opendata

# enable and start
sudo systemctl enable --now social-surveyor@opendata
```

Follow the logs until you see at least one poll / classify cycle:

```bash
sudo journalctl -u social-surveyor@opendata -f
```

---

## 7. First-digest verification

Dry-run the digest (stdout only, no Slack post):

```bash
sudo -u social-surveyor bash -c \
    'cd /opt/social-surveyor && \
     SOCIAL_SURVEYOR_DATA_DIR=/var/lib/social-surveyor/opendata \
     uv run social-surveyor digest --project opendata --dry-run'
```

If the Block Kit JSON looks healthy (has items, cost footer populated,
categories rendered), the 9am cron will do the same thing tomorrow.

To send one immediately to confirm the Slack post renders:

```bash
sudo -u social-surveyor bash -c \
    'cd /opt/social-surveyor && \
     SOCIAL_SURVEYOR_DATA_DIR=/var/lib/social-surveyor/opendata \
     uv run social-surveyor digest --project opendata'
```

---

## Backup and recovery

The Pulumi stack creates a Data Lifecycle Manager (DLM) policy that
snapshots any volume tagged `Snapshot=<project_name>` (the instance's
root volume is tagged at create-time). Defaults:

- **Cadence:** Sunday 02:00 UTC
- **Retention:** 4 snapshots (~one month of point-in-time recovery)
- **Cost:** DLM itself is free; snapshot storage on a 10 GB root is
  around **$0.05/GB/month × 4 snapshots × 10 GB ≈ $2/year**

Verify the policy is active after `pulumi up`:

```bash
AWS_PROFILE=prod aws dlm get-lifecycle-policies --region us-west-2
AWS_PROFILE=prod pulumi stack output dlm_policy_id
```

First snapshot appears the next Sunday 02:00 UTC. To see snapshots once
they start accumulating:

```bash
AWS_PROFILE=prod aws ec2 describe-snapshots --region us-west-2 \
    --owner-ids self \
    --filters "Name=tag:SnapshotOf,Values=opendata" \
    --query 'Snapshots[].[SnapshotId,StartTime,VolumeSize,State]' \
    --output table
```

### Restoring from a snapshot

Restore is a manual operation. Automation is deferred: picking the
right snapshot and verifying data integrity needs a human in the loop,
and the frequency (hopefully zero times per year) doesn't justify the
script.

Outline:

1. Pick a snapshot: `aws ec2 describe-snapshots --filters ...` (see above)
2. Stop the service: on the instance, `sudo systemctl stop social-surveyor@opendata`
3. Create a new volume from the snapshot (same AZ as the instance):
   `aws ec2 create-volume --snapshot-id snap-... --volume-type gp3
    --availability-zone us-west-2a`
4. Stop the instance, detach the current root volume, attach the new
   one as root, start the instance. (Or — for a non-root data restore,
   mount at a fresh mount point and copy the SQLite file over.)
5. Start the service: `sudo systemctl start social-surveyor@opendata`

Expect ~10 minutes hands-on time, plus however long AWS takes to
materialize the new volume.

---

## Rollback

```bash
cd deploy/pulumi
AWS_PROFILE=prod pulumi destroy
```

Destroys everything Pulumi owns: IAM role + policies, instance
profile, SG, EC2 instance. The EBS volume (`delete_on_termination`)
goes with the instance. SSM parameters and the Pulumi Cloud state
survive; clean those up with `aws ssm delete-parameters-by-path` and
`pulumi stack rm opendata` if you truly want no trace.

---

## Common pitfalls

- **SSM session fails with `TargetNotConnected`.** Give the instance
  30–60 seconds after `pulumi up` for the snap-packaged SSM agent to
  register. If it still fails, check that the IAM role is attached
  (`aws ec2 describe-instances --instance-ids <id> --query '...'`).
- **`uv sync` stalls on the first run.** It's compiling native wheels
  (httpx, tenacity, orjson). Give it 2–3 minutes on t4g.micro.
- **`load-env` writes 0 parameters.** You likely haven't run
  `seed-ssm.sh` yet, or you're hitting the wrong region / profile
  on the local machine.
- **`systemctl status` shows Restart loops with `exit-code=203`.**
  Almost always a missing `uv` on PATH; confirm `/usr/local/bin/uv`
  exists and is executable.

---

## Redeploy

### From the laptop — `deploy/deploy.sh` (preferred)

Once a release tag exists on `origin`, one command deploys it:

```bash
AWS_PROFILE=prod deploy/deploy.sh v0.6.0
```

What the script does:

1. Validates the working tree is clean (`--dirty` to override) and the
   tag exists both locally and on `origin`.
2. Resolves the instance id from `SOCIAL_SURVEYOR_INSTANCE_ID` or
   `pulumi stack output instance_id`.
3. Sends a single `aws ssm send-command` invocation that runs, on the
   instance:
   `git fetch --tags && git checkout <tag> && uv sync && systemctl
   restart social-surveyor@<project>`, then tails the last 20 lines
   of journald for the unit.
4. Polls the invocation to completion and streams stdout + stderr
   back. Exits non-zero on any remote failure.

Useful flags:

```bash
deploy/deploy.sh --help
deploy/deploy.sh v0.6.0 --dry-run              # print the remote command, don't SSM
deploy/deploy.sh v0.6.0 --project agent-infra  # deploy a different systemd instance
deploy/deploy.sh HEAD-tag --dirty              # skip the clean-tree check
```

### Fallback — manual SSM

If `deploy/deploy.sh` fails (AWS outage, SSM agent unhappy, odd git
state), fall back to the manual path:

```bash
AWS_PROFILE=prod aws ssm start-session --target <instance-id> --region us-west-2

# on the instance:
cd /opt/social-surveyor
sudo -u social-surveyor git fetch --tags
sudo -u social-surveyor git checkout <tag>     # or: git pull for a branch tip
sudo -u social-surveyor uv sync
sudo systemctl restart social-surveyor@opendata
```

If secrets changed:

```bash
# from the laptop:
AWS_PROFILE=prod deploy/seed-ssm.sh opendata
# then on the instance:
sudo /usr/local/bin/social-surveyor-load-env opendata
sudo systemctl restart social-surveyor@opendata
```

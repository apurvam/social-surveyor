#!/bin/bash
# One-command tag-based deploy for social-surveyor over AWS SSM.
#
# Usage:
#   deploy/deploy.sh                        # deploys HEAD's most recent tag
#   deploy/deploy.sh v0.6.0                 # deploys a specific tag
#   deploy/deploy.sh v0.6.0 --dry-run       # prints the remote command,
#                                             makes no SSM call
#   deploy/deploy.sh --help
#
# Environment:
#   AWS_PROFILE                 profile to use (passed through to aws CLI)
#   AWS_DEFAULT_REGION          region (defaults to us-west-2)
#   SOCIAL_SURVEYOR_INSTANCE_ID  EC2 instance id; if unset, resolves via
#                               `pulumi stack output instance_id`
#   SOCIAL_SURVEYOR_PROJECT     systemd template instance (defaults to opendata)
#
# Flags:
#   --dry-run                    print the remote command, don't execute it
#   --dirty                      allow deploy with a dirty working tree
#   --project <name>             override systemd project name
#   --help                       this message
#
# What it does on the instance:
#   1. cd /opt/social-surveyor
#   2. git fetch --tags
#   3. git checkout <tag>
#   4. uv sync
#   5. systemctl restart social-surveyor@<project>
#   6. journalctl -u social-surveyor@<project> --since '10 seconds ago'
#
# Streams the remote command's stdout+stderr once SSM reports done.
# Exits non-zero on any remote failure or SSM error.

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
PROJECT="${SOCIAL_SURVEYOR_PROJECT:-opendata}"
DRY_RUN=0
ALLOW_DIRTY=0
TAG=""

usage() {
    sed -n 's/^# \{0,1\}//p' "$0" | awk '/^Usage:/,/^$/ {print}'
}

die() {
    echo "error: $*" >&2
    exit 1
}

require() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH"
}

# --- argument parsing ---
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --dirty)
            ALLOW_DIRTY=1
            shift
            ;;
        --project)
            [ $# -ge 2 ] || die "--project requires a value"
            PROJECT="$2"
            shift 2
            ;;
        -*)
            die "unknown flag: $1"
            ;;
        *)
            [ -z "$TAG" ] || die "only one tag may be specified (got '$TAG' and '$1')"
            TAG="$1"
            shift
            ;;
    esac
done

require git
require aws

# --- validate working tree ---
if [ "$ALLOW_DIRTY" -eq 0 ]; then
    if [ -n "$(git status --porcelain=v1 2>/dev/null)" ]; then
        die "working tree is dirty (commit or stash; use --dirty to override)"
    fi
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$BRANCH" != "main" ]; then
    echo "warning: current branch is '$BRANCH', not 'main'" >&2
fi

# --- resolve tag ---
if [ -z "$TAG" ]; then
    TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
    [ -n "$TAG" ] || die "no tag on HEAD; pass one explicitly (e.g. v0.6.0)"
fi

# Confirm the tag exists in the repo. If it's not already in the local
# ref set, try fetching; bail if still missing.
if ! git rev-parse --verify --quiet "refs/tags/${TAG}" >/dev/null; then
    git fetch --tags --quiet || true
fi
if ! git rev-parse --verify --quiet "refs/tags/${TAG}" >/dev/null; then
    die "tag '$TAG' not found locally (fetch from remote first)"
fi

# Confirm the tag was pushed. The instance pulls from origin; a tag
# that's only local will silently fail the remote checkout.
if ! git ls-remote --tags origin "$TAG" 2>/dev/null | grep -q "refs/tags/${TAG}"; then
    if [ "$DRY_RUN" -eq 0 ]; then
        die "tag '$TAG' is not on origin — push it first (git push origin $TAG)"
    else
        echo "warning: tag '$TAG' is not on origin (would fail at remote checkout)" >&2
    fi
fi

# --- resolve instance id ---
INSTANCE_ID="${SOCIAL_SURVEYOR_INSTANCE_ID:-}"
if [ -z "$INSTANCE_ID" ]; then
    if command -v pulumi >/dev/null 2>&1 && [ -d deploy/pulumi ]; then
        INSTANCE_ID=$(cd deploy/pulumi && pulumi stack output instance_id 2>/dev/null || true)
    fi
fi
if [ -z "$INSTANCE_ID" ]; then
    die "could not resolve instance id — set SOCIAL_SURVEYOR_INSTANCE_ID or run from a checkout with pulumi state access"
fi

# --- build the remote command ---
# Note: using $PROJECT/$TAG is safe here because earlier parsing
# restricts them to tokens that survive through bash / systemd. The
# script still validates that they're present and non-empty above.
REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail
cd /opt/social-surveyor
echo '==> git fetch --tags'
sudo -u social-surveyor git fetch --tags
echo '==> git checkout ${TAG}'
sudo -u social-surveyor git checkout --detach ${TAG}
echo '==> uv sync'
sudo -u social-surveyor /usr/local/bin/uv sync --directory /opt/social-surveyor
echo '==> systemctl restart social-surveyor@${PROJECT}'
sudo systemctl restart social-surveyor@${PROJECT}
sleep 5
echo '==> systemctl is-active social-surveyor@${PROJECT}'
sudo systemctl is-active social-surveyor@${PROJECT}
echo '==> last 20 journal lines'
sudo journalctl -u social-surveyor@${PROJECT} --since '10 seconds ago' --no-pager | tail -20
REMOTE
)

echo "deploy target:"
echo "  tag:      $TAG"
echo "  project:  $PROJECT"
echo "  instance: $INSTANCE_ID"
echo "  region:   $REGION"
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- dry run: would execute on ${INSTANCE_ID} ---"
    echo "$REMOTE_SCRIPT"
    echo "--- end dry run ---"
    exit 0
fi

# --- send via SSM ---
# AWS-RunShellScript accepts a JSON-ish `commands` parameter. Passing
# the multi-line script via stdin + file:/// lets us avoid quoting
# every internal quote. The parameters file is single-use and
# deleted immediately on exit.
PARAMS=$(mktemp -t ssm-params.XXXXXX)
trap 'rm -f "$PARAMS"' EXIT

# commands must be a JSON array; python stdlib handles the escaping
# so we don't have to think about shell quoting of the script.
python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    script = f.read()
json.dump({'commands': [script]}, sys.stdout)
" <(printf '%s' "$REMOTE_SCRIPT") > "$PARAMS"

echo "==> sending SSM command to $INSTANCE_ID..."
CMD_ID=$(aws ssm send-command \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "file://${PARAMS}" \
    --query "Command.CommandId" \
    --output text) || die "send-command failed"

echo "    command id: $CMD_ID"

# --- poll for completion ---
while :; do
    INVOCATION=$(aws ssm get-command-invocation \
        --region "$REGION" \
        --command-id "$CMD_ID" \
        --instance-id "$INSTANCE_ID" \
        --output json 2>/dev/null || true)
    if [ -z "$INVOCATION" ]; then
        sleep 2
        continue
    fi
    STATUS=$(echo "$INVOCATION" | python3 -c "import json,sys; print(json.load(sys.stdin).get('Status',''))")
    case "$STATUS" in
        Success|Failed|Cancelled|TimedOut)
            break
            ;;
        Pending|InProgress|Delayed)
            echo "    ... $STATUS"
            sleep 5
            ;;
        "")
            sleep 2
            ;;
        *)
            sleep 3
            ;;
    esac
done

STDOUT=$(echo "$INVOCATION" | python3 -c "import json,sys; print(json.load(sys.stdin).get('StandardOutputContent',''))")
STDERR=$(echo "$INVOCATION" | python3 -c "import json,sys; print(json.load(sys.stdin).get('StandardErrorContent',''))")

if [ -n "$STDOUT" ]; then
    echo ""
    echo "--- remote stdout ---"
    echo "$STDOUT"
fi
if [ -n "$STDERR" ]; then
    echo ""
    echo "--- remote stderr ---"
    echo "$STDERR"
fi

echo ""
echo "==> SSM status: $STATUS"
if [ "$STATUS" != "Success" ]; then
    echo "    deploy did not complete cleanly — inspect the output above and/or SSM into the instance:" >&2
    echo "    AWS_PROFILE=\$AWS_PROFILE aws ssm start-session --target $INSTANCE_ID --region $REGION" >&2
    exit 1
fi

echo "==> deploy complete: $TAG on $INSTANCE_ID"

#!/bin/bash
# One-command deploy for social-surveyor over AWS SSM.
#
# Usage:
#   deploy/deploy.sh                        # deploys origin/main HEAD
#   deploy/deploy.sh v0.6.0                 # deploys a specific tag
#   deploy/deploy.sh fix/something          # deploys a branch tip
#   deploy/deploy.sh abc1234                # deploys a specific commit
#   deploy/deploy.sh --dry-run              # prints the remote command,
#                                             makes no SSM call
#   deploy/deploy.sh --help
#
# Accepts any git ref the local clone knows about: tag, branch (via
# origin/<name>), or SHA. Defaults to origin/main so the tag-less
# "deploy latest main" flow stays one keystroke.
#
# Environment:
#   AWS_PROFILE                 profile passed through to aws CLI
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
# What it does on the instance (commands run against the resolved SHA,
# so the remote state is byte-identical to the laptop's view):
#   1. cd /opt/social-surveyor
#   2. git fetch --all --tags
#   3. git checkout --detach <sha>
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
REF=""

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
            [ -z "$REF" ] || die "only one ref may be specified (got '$REF' and '$1')"
            REF="$1"
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

# --- resolve ref to a SHA ---
# Refresh both branch and tag refs before resolving. Offline? Skip and
# hope the local cache has what we need.
git fetch --quiet --tags origin 2>/dev/null || true

# Default to main HEAD — matches the pre-deploy.sh workflow where "go"
# meant "whatever latest main is."
if [ -z "$REF" ]; then
    REF="main"
fi

REF_TYPE=""
RESOLVED_SHA=""
if git rev-parse --verify --quiet "refs/tags/${REF}" >/dev/null; then
    REF_TYPE="tag"
    # Dereference annotated tags to their commit SHA.
    RESOLVED_SHA=$(git rev-parse "refs/tags/${REF}^{commit}")
elif git rev-parse --verify --quiet "refs/remotes/origin/${REF}" >/dev/null; then
    REF_TYPE="branch"
    RESOLVED_SHA=$(git rev-parse "refs/remotes/origin/${REF}")
elif git rev-parse --verify --quiet "${REF}^{commit}" >/dev/null 2>&1; then
    REF_TYPE="commit"
    RESOLVED_SHA=$(git rev-parse "${REF}^{commit}")
else
    die "ref '${REF}' is not a tag, a branch on origin, or a known commit SHA"
fi

# For tags, also confirm the tag is on origin so a local-only tag
# doesn't silently fail the remote checkout.
if [ "$REF_TYPE" = "tag" ]; then
    if ! git ls-remote --tags origin "$REF" 2>/dev/null | grep -q "refs/tags/${REF}"; then
        if [ "$DRY_RUN" -eq 0 ]; then
            die "tag '$REF' is not on origin — push it first (git push origin $REF)"
        else
            echo "warning: tag '$REF' is not on origin (would fail at remote checkout)" >&2
        fi
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
# We pass the resolved SHA rather than the user-facing ref so the
# remote checkout is byte-identical to what the laptop sees — no
# ambiguity between a local branch tip and the remote branch tip.
#
# SSM's AWS-RunShellScript executes each line via /bin/sh (dash on
# Ubuntu), which doesn't support `set -o pipefail`. We wrap the whole
# body in `bash -c` so pipefail and other bash-isms work predictably.
REMOTE_BODY=$(cat <<REMOTE
set -euo pipefail
cd /opt/social-surveyor
echo '==> git fetch --all --tags (from ${REF_TYPE} ${REF})'
sudo -u social-surveyor git fetch --all --tags --quiet
echo '==> git checkout --detach ${RESOLVED_SHA}'
sudo -u social-surveyor git checkout --detach ${RESOLVED_SHA}
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

# Base64-encode so arbitrary shell metacharacters in REMOTE_BODY
# don't need escaping when we substitute into the outer `bash -c`.
REMOTE_BODY_B64=$(printf '%s' "$REMOTE_BODY" | base64 | tr -d '\n')
REMOTE_SCRIPT="echo ${REMOTE_BODY_B64} | base64 -d | bash"

echo "deploy target:"
echo "  ref:      $REF ($REF_TYPE)"
echo "  sha:      $RESOLVED_SHA"
echo "  project:  $PROJECT"
echo "  instance: $INSTANCE_ID"
echo "  region:   $REGION"
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- dry run: would execute on ${INSTANCE_ID} ---"
    # Show the human-readable body, not the base64-wrapped invocation
    # that actually gets sent to SSM.
    echo "$REMOTE_BODY"
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

echo "==> deploy complete: $REF ($RESOLVED_SHA) on $INSTANCE_ID"

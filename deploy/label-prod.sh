#!/bin/bash
# One-command labeling session against a prod project DB.
#
# Usage:
#   deploy/label-prod.sh --project <name>
#   deploy/label-prod.sh --project <name> --dry-run
#   deploy/label-prod.sh --help
#
# What it does:
#   1. SSM the prod instance to upload its SQLite DB for <project> to
#      a presigned S3 PUT URL (no IAM change on the instance role).
#   2. aws s3 cp the staged DB down to data/<project>.db.
#   3. aws s3 rm the staged object.
#   4. Launch `uv run social-surveyor label --project <name>`
#      interactively against the fetched DB.
#   5. If projects/<name>/evals/labeled.jsonl changed, create a
#      labels/<name>-<stamp> branch, commit the delta, push, and open
#      a PR via gh. The operator merges + runs deploy/deploy.sh to
#      persist the labels on prod.
#
# Flags:
#   --project <name>       required; project directory name
#   --bucket <name>        S3 staging bucket; default
#                           social-surveyor-label-staging-<accountid>
#                           (created on first run with a 1-day object
#                           expiry lifecycle rule)
#   --dry-run              print the plan without touching SSM/S3/git
#   --dirty                allow a dirty working tree at start
#   --help
#
# Environment:
#   AWS_PROFILE                  aws profile (inherit from caller)
#   AWS_DEFAULT_REGION           region (defaults to us-west-2)
#   SOCIAL_SURVEYOR_INSTANCE_ID  EC2 id; if unset, resolves via
#                                `pulumi stack output instance_id`

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-west-2}"
DRY_RUN=0
ALLOW_DIRTY=0
PROJECT=""
BUCKET=""

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
        --bucket)
            [ $# -ge 2 ] || die "--bucket requires a value"
            BUCKET="$2"
            shift 2
            ;;
        -*)
            die "unknown flag: $1"
            ;;
        *)
            die "unexpected argument: $1"
            ;;
    esac
done

[ -n "$PROJECT" ] || die "--project is required"

require git
require aws
require uv

# --- working tree ---
if [ "$ALLOW_DIRTY" -eq 0 ]; then
    if [ -n "$(git status --porcelain=v1 2>/dev/null)" ]; then
        die "working tree is dirty (commit or stash; use --dirty to override)"
    fi
fi

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) \
    || die "not inside a git repo"

PROJECT_DIR="${REPO_ROOT}/projects/${PROJECT}"
[ -d "$PROJECT_DIR" ] || die "project dir not found: $PROJECT_DIR"

# --- resolve instance + bucket ---
INSTANCE_ID="${SOCIAL_SURVEYOR_INSTANCE_ID:-}"
if [ -z "$INSTANCE_ID" ]; then
    if command -v pulumi >/dev/null 2>&1 && [ -d "${REPO_ROOT}/deploy/pulumi" ]; then
        INSTANCE_ID=$(cd "${REPO_ROOT}/deploy/pulumi" && pulumi stack output instance_id 2>/dev/null || true)
    fi
fi
[ -n "$INSTANCE_ID" ] \
    || die "could not resolve instance id — set SOCIAL_SURVEYOR_INSTANCE_ID or run with pulumi state access"

if [ -z "$BUCKET" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        # Defer STS in dry-run so the summary still works without creds.
        BUCKET="social-surveyor-label-staging-<accountid>"
    else
        ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
            || die "aws sts get-caller-identity failed — check AWS_PROFILE=${AWS_PROFILE:-<unset>}"
        BUCKET="social-surveyor-label-staging-${ACCOUNT_ID}"
    fi
fi

# --- paths ---
REMOTE_DB="/var/lib/social-surveyor/${PROJECT}/${PROJECT}.db"
LOCAL_DATA_DIR="${REPO_ROOT}/data"
LOCAL_DB="${LOCAL_DATA_DIR}/${PROJECT}.db"
LABELS_FILE="projects/${PROJECT}/evals/labeled.jsonl"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
S3_KEY="db-snapshots/${PROJECT}/${STAMP}-${RANDOM}.db"

echo "label session target:"
echo "  project:   $PROJECT"
echo "  instance:  $INSTANCE_ID"
echo "  remote db: $REMOTE_DB"
echo "  s3 stage:  s3://$BUCKET/$S3_KEY"
echo "  local db:  $LOCAL_DB"
echo "  region:    $REGION"
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- dry run: would ---"
    echo "1. ensure S3 staging bucket '$BUCKET' exists (1-day object expiry)"
    echo "2. generate presigned PUT URL for s3://$BUCKET/$S3_KEY (300s ttl)"
    echo "3. SSM send-command to $INSTANCE_ID: curl --upload-file $REMOTE_DB to the presigned URL"
    echo "4. aws s3 cp s3://$BUCKET/$S3_KEY $LOCAL_DB"
    echo "5. aws s3 rm s3://$BUCKET/$S3_KEY"
    echo "6. uv run social-surveyor label --project $PROJECT"
    echo "7. if $LABELS_FILE changed: branch labels/${PROJECT}-${STAMP}, commit, push, gh pr create"
    echo "--- end dry run ---"
    exit 0
fi

# --- ensure staging bucket exists (idempotent) ---
if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
    echo "==> creating S3 staging bucket '$BUCKET' in $REGION"
    # us-east-1 doesn't accept LocationConstraint; everywhere else does.
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
    else
        aws s3api create-bucket \
            --bucket "$BUCKET" \
            --region "$REGION" \
            --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-public-access-block \
        --bucket "$BUCKET" \
        --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
        >/dev/null
    aws s3api put-bucket-lifecycle-configuration \
        --bucket "$BUCKET" \
        --lifecycle-configuration '{"Rules":[{"ID":"expire-1d","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":1}}]}' \
        >/dev/null
fi

# --- presigned PUT URL (so the instance role doesn't need S3 perms) ---
echo "==> generating presigned PUT URL (expires 300s)"
PRESIGNED_URL=$(aws s3 presign "s3://$BUCKET/$S3_KEY" \
    --expires-in 300 \
    --region "$REGION" \
    --http-method PUT)

# --- SSM: instance uploads DB to the presigned URL ---
REMOTE_BODY=$(cat <<REMOTE
set -euo pipefail
test -f '${REMOTE_DB}' || { echo "not found: ${REMOTE_DB}" >&2; exit 2; }
curl --fail --silent --show-error --upload-file '${REMOTE_DB}' '${PRESIGNED_URL}'
echo "uploaded: ${REMOTE_DB}"
REMOTE
)
REMOTE_BODY_B64=$(printf '%s' "$REMOTE_BODY" | base64 | tr -d '\n')
REMOTE_SCRIPT="echo ${REMOTE_BODY_B64} | base64 -d | bash"

PARAMS=$(mktemp -t ssm-params.XXXXXX)
trap 'rm -f "$PARAMS"' EXIT

python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    script = f.read()
json.dump({'commands': [script]}, sys.stdout)
" <(printf '%s' "$REMOTE_SCRIPT") > "$PARAMS"

echo "==> SSM upload: $REMOTE_DB  →  s3://$BUCKET/$S3_KEY"
CMD_ID=$(aws ssm send-command \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "file://${PARAMS}" \
    --query "Command.CommandId" \
    --output text) || die "send-command failed"

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
    STATUS=$(echo "$INVOCATION" \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('Status',''))")
    case "$STATUS" in
        Success|Failed|Cancelled|TimedOut) break ;;
        Pending|InProgress|Delayed) sleep 3 ;;
        *) sleep 2 ;;
    esac
done

if [ "$STATUS" != "Success" ]; then
    STDERR=$(echo "$INVOCATION" \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('StandardErrorContent',''))")
    die "remote upload failed (status=$STATUS): ${STDERR:-<no stderr>}"
fi

# --- pull + cleanup ---
mkdir -p "$LOCAL_DATA_DIR"
echo "==> aws s3 cp s3://$BUCKET/$S3_KEY $LOCAL_DB"
aws s3 cp "s3://$BUCKET/$S3_KEY" "$LOCAL_DB" --region "$REGION" --quiet

echo "==> aws s3 rm s3://$BUCKET/$S3_KEY"
aws s3 rm "s3://$BUCKET/$S3_KEY" --region "$REGION" --quiet || true

DB_SIZE=$(wc -c < "$LOCAL_DB" | tr -d ' ')
echo "    local DB size: ${DB_SIZE} bytes"

# --- snapshot labels file for change detection ---
LABELS_PRE_SHA=""
if [ -f "$REPO_ROOT/$LABELS_FILE" ]; then
    LABELS_PRE_SHA=$(git -C "$REPO_ROOT" hash-object "$LABELS_FILE")
fi

# --- interactive labeler ---
echo ""
echo "==> launching labeler (per-decision autosave; 'q' to exit)"
echo ""
cd "$REPO_ROOT"
# Don't let a non-zero exit (Ctrl-C / 'q' path) skip the commit block;
# the labeler is append-only so anything written is still valid.
uv run social-surveyor label --project "$PROJECT" || true

# --- commit + PR on change ---
if [ ! -f "$LABELS_FILE" ]; then
    echo ""
    echo "==> no labels file at $LABELS_FILE — nothing to commit."
    exit 0
fi

LABELS_POST_SHA=$(git -C "$REPO_ROOT" hash-object "$LABELS_FILE")
if [ "$LABELS_PRE_SHA" = "$LABELS_POST_SHA" ]; then
    echo ""
    echo "==> $LABELS_FILE unchanged — nothing to commit."
    exit 0
fi

# New-file case vs. delta case.
if [ -z "$LABELS_PRE_SHA" ]; then
    ADDED=$(wc -l < "$LABELS_FILE" | tr -d ' ')
else
    # --numstat gives "added\tremoved\tpath"; labels are append-only so
    # removed is 0 and added is the new-row count.
    ADDED=$(git -C "$REPO_ROOT" diff --numstat -- "$LABELS_FILE" | awk '{print $1}')
fi
ADDED=${ADDED:-0}

BRANCH="labels/${PROJECT}-${STAMP}"
echo ""
echo "==> branching $BRANCH and committing ${ADDED} label line(s)"
git -C "$REPO_ROOT" checkout -b "$BRANCH"
git -C "$REPO_ROOT" add "$LABELS_FILE"
git -C "$REPO_ROOT" commit -m "chore(labels): add ${ADDED} labels from prod session (${PROJECT})" \
    -m "Captured via deploy/label-prod.sh against the prod ${PROJECT} DB."

echo "==> git push -u origin $BRANCH"
git -C "$REPO_ROOT" push -u origin "$BRANCH"

if command -v gh >/dev/null 2>&1; then
    echo "==> opening PR via gh"
    gh pr create \
        --title "chore(labels): ${ADDED} new labels for ${PROJECT}" \
        --body "Captured via \`deploy/label-prod.sh --project ${PROJECT}\` against the prod DB.

- File: \`${LABELS_FILE}\`
- Added rows: ${ADDED}
- Merge + \`deploy/deploy.sh --project ${PROJECT}\` to persist the labels on prod."
else
    echo "    gh CLI not found; open the PR manually for branch $BRANCH"
fi

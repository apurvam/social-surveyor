#!/bin/bash
# Seed SSM Parameter Store from a local .env file.
# Run this once from the operator laptop before the first deploy.
#
# Usage:
#   deploy/seed-ssm.sh <project-name>        # defaults SOCIAL_SURVEYOR_ENV_FILE=.env
#   SOCIAL_SURVEYOR_ENV_FILE=.env.prod deploy/seed-ssm.sh opendata
#
# Honors AWS_PROFILE and AWS_DEFAULT_REGION from the caller's environment.

set -euo pipefail

PROJECT="${1:-opendata}"
PREFIX="/social-surveyor/${PROJECT}"
ENV_FILE="${SOCIAL_SURVEYOR_ENV_FILE:-.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found" >&2
    exit 1
fi

echo "Project:       $PROJECT"
echo "SSM prefix:    $PREFIX"
echo "Env file:      $ENV_FILE"
echo "AWS profile:   ${AWS_PROFILE:-<default>}"
echo "AWS region:    ${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo '<unset>')}"
echo ""
echo "This will write every KEY=VALUE line from $ENV_FILE to SSM Parameter Store"
echo "under $PREFIX/<KEY> as SecureString (overwriting existing values)."
read -r -p "Continue? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Aborted"
    exit 0
fi

count=0
while IFS= read -r line || [ -n "$line" ]; do
    # Skip blank lines and comments
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac

    # Split on first '=' only — values can contain '='
    key="${line%%=*}"
    value="${line#*=}"

    # Trim whitespace on key; bail if malformed
    key="${key## }"
    key="${key%% }"
    if [ -z "$key" ] || [ "$key" = "$line" ]; then
        continue
    fi

    # Strip matching surrounding quotes on value (single or double)
    if [ "${value#\"}" != "$value" ] && [ "${value%\"}" != "$value" ]; then
        value="${value#\"}"
        value="${value%\"}"
    elif [ "${value#\'}" != "$value" ] && [ "${value%\'}" != "$value" ]; then
        value="${value#\'}"
        value="${value%\'}"
    fi

    printf '  writing %s/%s ... ' "$PREFIX" "$key"
    aws ssm put-parameter \
        --name "${PREFIX}/${key}" \
        --value "$value" \
        --type SecureString \
        --overwrite \
        --no-cli-pager > /dev/null
    printf 'ok\n'
    count=$((count + 1))
done < "$ENV_FILE"

echo ""
echo "Wrote $count parameters."
echo "Verify with: aws ssm get-parameters-by-path --path ${PREFIX} --query 'Parameters[*].Name' --output table"

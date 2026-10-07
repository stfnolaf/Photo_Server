#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bin="$tmp/bin"
mkdir -p "$bin"

cat >"$bin/pg_isready" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
while (($#)); do
  if [[ "$1" == --file ]]; then printf 'verified postgres dump\n' >"$2"; break; fi
  shift
done
EOF
cat >"$bin/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
endpoint=""
args=()
while (($#)); do
  if [[ "$1" == --endpoint-url ]]; then endpoint="$2"; shift 2
  elif [[ "$1" == --no-sign-request ]]; then shift
  else args+=("$1"); shift
  fi
done
operation="${args[1]:-}"
role=primary
[[ "$endpoint" == secondary ]] && role=secondary
role_upper="${role^^}"
fail_var="MOCK_${role_upper}_FAIL"
bucket_var="MOCK_${role_upper}_BUCKET_EXISTS"
checksum_var="MOCK_${role_upper}_BAD_CHECKSUM"
if [[ "${!fail_var:-false}" == true ]]; then exit 1; fi
case "$operation" in
  head-bucket) [[ "${!bucket_var:-false}" == true ]] ;;
  create-bucket|put-object|delete-object) : ;;
  head-object)
    if [[ "${!checksum_var:-false}" == true ]]; then echo bad-checksum; else echo "$MOCK_DIGEST"; fi
    ;;
  list-objects-v2) echo None ;;
  *) echo "unexpected mock operation: $operation" >&2; exit 2 ;;
esac
EOF
chmod +x "$bin"/*

run_backup() {
  local name="$1" secondary_endpoint_value="$2" secondary_path_value="$3" required="$4"
  local status="$tmp/$name-status.json"
  shift 4
  if env PATH="$bin:$PATH" \
    PGHOST=postgres PGUSER=photo PGDATABASE=photo PGPASSWORD=redacted \
    PHOTO_S3_ENDPOINT=primary PHOTO_S3_BUCKET=photos PHOTO_S3_ANONYMOUS=true \
    PHOTO_POSTGRES_BACKUP_STATUS_PATH="$status" PHOTO_POSTGRES_BACKUP_SECONDARY_ENDPOINT="$secondary_endpoint_value" \
    PHOTO_POSTGRES_BACKUP_SECONDARY_PATH="$secondary_path_value" PHOTO_POSTGRES_BACKUP_SECONDARY_RETENTION=1 PHOTO_POSTGRES_BACKUP_SECONDARY_REQUIRED="$required" \
    MOCK_DIGEST="$(printf 'verified postgres dump\n' | sha256sum | awk '{print $1}')" "$@" \
    bash "$root/ops/postgres-backup/backup.sh" once >/dev/null; then
      local result=0
    else
      local result=$?
    fi
  printf '%s\n' "$status"
  return "$result"
}

assert_status() {
  local file="$1" overall="$2" primary="$3" secondary="$4" error_class="${5:-}"
  grep -q '"overallStatus":"'"$overall"'"' "$file"
  grep -q '"primary":{"status":"'"$primary"'"' "$file"
  grep -q '"secondary":{"status":"'"$secondary"'"' "$file"
  [[ -z "$error_class" ]] || grep -q '"errorClass":"'"$error_class"'"' "$file"
}

status="$(run_backup both-success secondary '' false MOCK_PRIMARY_BUCKET_EXISTS=true MOCK_SECONDARY_BUCKET_EXISTS=true)"
assert_status "$status" healthy success success

status="$(run_backup primary-failure secondary '' false MOCK_PRIMARY_FAIL=true MOCK_SECONDARY_BUCKET_EXISTS=true)" || true
assert_status "$status" degraded failed success UploadError

status="$(run_backup secondary-failure secondary '' true MOCK_PRIMARY_BUCKET_EXISTS=true MOCK_SECONDARY_FAIL=true)" || true
assert_status "$status" degraded success failed UploadError

status="$(run_backup checksum-mismatch secondary '' true MOCK_PRIMARY_BUCKET_EXISTS=true MOCK_SECONDARY_BUCKET_EXISTS=true MOCK_SECONDARY_BAD_CHECKSUM=true)" || true
assert_status "$status" degraded success failed ChecksumMismatch

nas="$tmp/nas"
for run in one two; do
  status="$(run_backup "retention-$run" '' "$nas" false MOCK_PRIMARY_BUCKET_EXISTS=true)"
done
[[ "$(find "$nas" -type f -name '*.dump' | wc -l)" -eq 1 ]]

echo "backup scenarios passed"

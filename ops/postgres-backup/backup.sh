#!/usr/bin/env bash
set -euo pipefail

: "${PHOTO_S3_ENDPOINT:?PHOTO_S3_ENDPOINT is required}"
: "${PHOTO_S3_BUCKET:?PHOTO_S3_BUCKET is required}"
: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

prefix="${PHOTO_POSTGRES_BACKUP_PREFIX:-backups/postgres}"
interval="${PHOTO_POSTGRES_BACKUP_INTERVAL_SECONDS:-3600}"
retention="${PHOTO_POSTGRES_BACKUP_RETENTION:-168}"
secondary_retention="${PHOTO_POSTGRES_BACKUP_SECONDARY_RETENTION:-$retention}"
status_path="${PHOTO_POSTGRES_BACKUP_STATUS_PATH:-/var/lib/photo-backup/status.json}"
secondary_endpoint="${PHOTO_POSTGRES_BACKUP_SECONDARY_ENDPOINT:-}"
secondary_path="${PHOTO_POSTGRES_BACKUP_SECONDARY_PATH:-}"
secondary_required="${PHOTO_POSTGRES_BACKUP_SECONDARY_REQUIRED:-false}"

s3() {
    if [[ "${PHOTO_S3_ANONYMOUS:-true}" == true ]]; then
        aws --endpoint-url "$PHOTO_S3_ENDPOINT" --no-sign-request "$@"
    else aws --endpoint-url "$PHOTO_S3_ENDPOINT" "$@"; fi
}
secondary_s3() {
    local -a args=(--endpoint-url "$secondary_endpoint")
    [[ "${PHOTO_POSTGRES_BACKUP_SECONDARY_ANONYMOUS:-false}" == true ]] && args+=(--no-sign-request)
    AWS_ACCESS_KEY_ID="${PHOTO_POSTGRES_BACKUP_SECONDARY_ACCESS_KEY_ID:-}" \
    AWS_SECRET_ACCESS_KEY="${PHOTO_POSTGRES_BACKUP_SECONDARY_SECRET_ACCESS_KEY:-}" \
    AWS_SESSION_TOKEN="${PHOTO_POSTGRES_BACKUP_SECONDARY_SESSION_TOKEN:-}" aws "${args[@]}" "$@"
}
write_status() {
    local overall="$1" primary="$2" secondary="$3" dir tmp
    dir="$(dirname "$status_path")"; mkdir -p "$dir"; tmp="$(mktemp "$dir/.status.XXXXXX")"
    printf '{"updatedAt":"%s","overallStatus":"%s","primary":%s,"secondary":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$overall" "$primary" "$secondary" >"$tmp"
    mv -f "$tmp" "$status_path"
}
wait_for_database() {
    until pg_isready --quiet --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" --dbname "$PGDATABASE"; do sleep 2; done
}
prune_s3() {
    local bucket="$1" keep="$2" command="$3"; local -a keys
    mapfile -t keys < <($command s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix/" --query 'sort_by(Contents,&LastModified)[].Key' --output text | tr '\t' '\n' | sed '/^None$/d;/^$/d')
    local count=$((${#keys[@]} - keep)); ((count > 0)) || return 0
    for ((i=0; i<count; i++)); do $command s3api delete-object --bucket "$bucket" --key "${keys[$i]}" >/dev/null; done
}
prune_files() {
    local root="$1" keep="$2"; local -a files
    mapfile -t files < <(find "$root" -type f -name '*.dump' -printf '%T@ %p\n' | sort -n | cut -d' ' -f2-)
    local count=$((${#files[@]} - keep)); ((count > 0)) || return 0
    for ((i=0; i<count; i++)); do rm -f -- "${files[$i]}" "${files[$i]}.sha256"; done
}
primary_upload() {
    local file="$1" key="$2" digest="$3" expected
    if ! s3 s3api head-bucket --bucket "$PHOTO_S3_BUCKET" >/dev/null 2>&1; then s3 s3api create-bucket --bucket "$PHOTO_S3_BUCKET" >/dev/null; fi
    s3 s3api put-object --bucket "$PHOTO_S3_BUCKET" --key "$key" --body "$file" --content-type application/vnd.postgresql.dump --metadata "sha256=$digest,database=$PGDATABASE" >/dev/null
    expected="$(s3 s3api head-object --bucket "$PHOTO_S3_BUCKET" --key "$key" --query 'Metadata.sha256' --output text)"
    [[ "$expected" == "$digest" ]] || return 42
    prune_s3 "$PHOTO_S3_BUCKET" "$retention" s3
}
secondary_upload() {
    local file="$1" key="$2" digest="$3" expected bucket destination
    if [[ -n "$secondary_endpoint" ]]; then
        bucket="${PHOTO_POSTGRES_BACKUP_SECONDARY_BUCKET:-postgres-backups}"
        if ! secondary_s3 s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then secondary_s3 s3api create-bucket --bucket "$bucket" >/dev/null; fi
        secondary_s3 s3api put-object --bucket "$bucket" --key "$key" --body "$file" --content-type application/vnd.postgresql.dump --metadata "sha256=$digest,database=$PGDATABASE" >/dev/null
        expected="$(secondary_s3 s3api head-object --bucket "$bucket" --key "$key" --query 'Metadata.sha256' --output text)"
        [[ "$expected" == "$digest" ]] || return 42
        prune_s3 "$bucket" "$secondary_retention" secondary_s3
    elif [[ -n "$secondary_path" ]]; then
        destination="$secondary_path/$key"; mkdir -p "$(dirname "$destination")"; cp -- "$file" "$destination"
        printf '%s  %s\n' "$digest" "$(basename "$destination")" >"$destination.sha256"
        [[ "$(sha256sum "$destination" | awk '{print $1}')" == "$digest" ]] || return 42
        prune_files "$secondary_path" "$secondary_retention"
    else return 44; fi
}
backup_once() {
    wait_for_database
    local file timestamp key digest size primary secondary overall
    file="$(mktemp /tmp/photo-postgres.XXXXXX.dump)"; trap 'rm -f "${file:-}"' EXIT
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"; key="$prefix/$timestamp-$(tr -d '\n' </proc/sys/kernel/random/uuid).dump"
    pg_dump --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" --dbname "$PGDATABASE" --format custom --compress 6 --no-owner --no-acl --file "$file"
    digest="$(sha256sum "$file" | awk '{print $1}')"; size="$(stat --format='%s' "$file")"
    primary='{"status":"failed","errorClass":"UploadError"}'; secondary='{"status":"not-configured"}'
    if primary_upload "$file" "$key" "$digest"; then primary="{\"status\":\"success\",\"key\":\"$key\",\"at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"verified\":true}"; fi
    if [[ -n "$secondary_endpoint$secondary_path" ]]; then
        secondary='{"status":"failed","errorClass":"UploadError"}'
        if secondary_upload "$file" "$key" "$digest"; then secondary="{\"status\":\"success\",\"key\":\"$key\",\"at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"verified\":true}"; fi
    fi
    if [[ "$primary" == *'"status":"success"'* ]] && { [[ "$secondary" == *'"status":"success"'* ]] || { [[ "$secondary" == *'"status":"not-configured"'* ]] && [[ "$secondary_required" != true ]]; }; }; then overall=healthy; else overall=degraded; fi
    write_status "$overall" "$primary" "$secondary"
    printf '{"status":"%s","key":"%s","bytes":%s,"sha256":"%s"}\n' "$overall" "$key" "$size" "$digest"
    rm -f "$file"; trap - EXIT; [[ "$overall" == healthy ]]
}
restore_backup() {
    local key="${1:?Pass the S3 backup key to restore}"; [[ "${PHOTO_RESTORE_CONFIRM:-}" == "$PGDATABASE" ]] || { echo "Set PHOTO_RESTORE_CONFIRM=$PGDATABASE to confirm destructive restore" >&2; exit 2; }
    wait_for_database; local file expected actual; file="$(mktemp /tmp/photo-postgres-restore.XXXXXX.dump)"; trap 'rm -f "${file:-}"' EXIT
    s3 s3api get-object --bucket "$PHOTO_S3_BUCKET" --key "$key" "$file" >/dev/null; expected="$(s3 s3api head-object --bucket "$PHOTO_S3_BUCKET" --key "$key" --query 'Metadata.sha256' --output text)"; actual="$(sha256sum "$file" | awk '{print $1}')"
    [[ "$expected" != None && "$actual" == "$expected" ]] || { echo "Backup checksum verification failed" >&2; exit 1; }
    dropdb --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" --force --if-exists "$PGDATABASE"; createdb --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" "$PGDATABASE"; pg_restore --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" --dbname "$PGDATABASE" --exit-on-error --no-owner --no-acl "$file"
    printf '{"status":"restored","key":"%s","sha256":"%s"}\n' "$key" "$actual"; rm -f "$file"; trap - EXIT
}
case "${1:-loop}" in
    once) backup_once ;; loop) while true; do backup_once || true; sleep "$interval"; done ;; restore) shift; restore_backup "$@" ;; *) echo "Usage: photo-postgres-backup {once|loop|restore S3_KEY}" >&2; exit 2 ;;
esac

#!/usr/bin/env bash
set -euo pipefail

: "${PHOTO_S3_ENDPOINT:?PHOTO_S3_ENDPOINT is required}"
: "${PHOTO_S3_BUCKET:?PHOTO_S3_BUCKET is required}"
: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

backup_prefix="${PHOTO_POSTGRES_BACKUP_PREFIX:-backups/postgres}"
interval="${PHOTO_POSTGRES_BACKUP_INTERVAL_SECONDS:-3600}"
retention="${PHOTO_POSTGRES_BACKUP_RETENTION:-168}"

s3() {
    if [[ "${PHOTO_S3_ANONYMOUS:-true}" == "true" ]]; then
        aws --endpoint-url "$PHOTO_S3_ENDPOINT" --no-sign-request "$@"
    else
        aws --endpoint-url "$PHOTO_S3_ENDPOINT" "$@"
    fi
}

wait_for_database() {
    local database="${1:-$PGDATABASE}"
    until pg_isready --quiet --host "$PGHOST" --port "${PGPORT:-5432}" \
        --username "$PGUSER" --dbname "$database"; do
        sleep 2
    done
}

ensure_bucket() {
    if ! s3 s3api head-bucket --bucket "$PHOTO_S3_BUCKET" >/dev/null 2>&1; then
        s3 s3api create-bucket --bucket "$PHOTO_S3_BUCKET" >/dev/null
    fi
}

prune_backups() {
    mapfile -t keys < <(
        s3 s3api list-objects-v2 \
            --bucket "$PHOTO_S3_BUCKET" \
            --prefix "$backup_prefix/" \
            --query 'sort_by(Contents,&LastModified)[].Key' \
            --output text | tr '\t' '\n' | sed '/^None$/d;/^$/d'
    )
    local remove_count=$((${#keys[@]} - retention))
    if ((remove_count <= 0)); then
        return
    fi
    for ((index = 0; index < remove_count; index++)); do
        s3 s3api delete-object --bucket "$PHOTO_S3_BUCKET" --key "${keys[$index]}" >/dev/null
    done
}

backup_once() {
    wait_for_database
    ensure_bucket
    local temporary timestamp identifier key digest size
    temporary="$(mktemp /tmp/photo-postgres.XXXXXX.dump)"
    trap 'rm -f "${temporary:-}"' EXIT
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    identifier="$(tr -d '\n' </proc/sys/kernel/random/uuid)"
    key="$backup_prefix/$timestamp-$identifier.dump"
    pg_dump \
        --host "$PGHOST" \
        --port "${PGPORT:-5432}" \
        --username "$PGUSER" \
        --dbname "$PGDATABASE" \
        --format custom \
        --compress 6 \
        --no-owner \
        --no-acl \
        --file "$temporary"
    digest="$(sha256sum "$temporary" | awk '{print $1}')"
    size="$(stat --format='%s' "$temporary")"
    s3 s3api put-object \
        --bucket "$PHOTO_S3_BUCKET" \
        --key "$key" \
        --body "$temporary" \
        --content-type application/vnd.postgresql.dump \
        --metadata "sha256=$digest,database=$PGDATABASE" >/dev/null
    prune_backups
    printf '{"status":"backed_up","key":"%s","bytes":%s,"sha256":"%s"}\n' \
        "$key" "$size" "$digest"
    rm -f "$temporary"
    trap - EXIT
}

restore_backup() {
    local key="${1:?Pass the S3 backup key to restore}"
    if [[ "${PHOTO_RESTORE_CONFIRM:-}" != "$PGDATABASE" ]]; then
        echo "Set PHOTO_RESTORE_CONFIRM=$PGDATABASE to confirm destructive restore" >&2
        exit 2
    fi
    wait_for_database postgres
    local temporary expected actual
    temporary="$(mktemp /tmp/photo-postgres-restore.XXXXXX.dump)"
    trap 'rm -f "${temporary:-}"' EXIT
    s3 s3api get-object --bucket "$PHOTO_S3_BUCKET" --key "$key" "$temporary" >/dev/null
    expected="$(s3 s3api head-object --bucket "$PHOTO_S3_BUCKET" --key "$key" \
        --query 'Metadata.sha256' --output text)"
    actual="$(sha256sum "$temporary" | awk '{print $1}')"
    if [[ "$expected" == "None" || "$actual" != "$expected" ]]; then
        echo "Backup checksum verification failed" >&2
        exit 1
    fi
    dropdb --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" \
        --force --if-exists "$PGDATABASE"
    createdb --host "$PGHOST" --port "${PGPORT:-5432}" --username "$PGUSER" "$PGDATABASE"
    pg_restore \
        --host "$PGHOST" \
        --port "${PGPORT:-5432}" \
        --username "$PGUSER" \
        --dbname "$PGDATABASE" \
        --exit-on-error \
        --no-owner \
        --no-acl \
        "$temporary"
    printf '{"status":"restored","key":"%s","sha256":"%s"}\n' "$key" "$actual"
    rm -f "$temporary"
    trap - EXIT
}

case "${1:-loop}" in
    once)
        backup_once
        ;;
    loop)
        while true; do
            backup_once
            sleep "$interval"
        done
        ;;
    restore)
        shift
        restore_backup "$@"
        ;;
    *)
        echo "Usage: photo-postgres-backup {once|loop|restore S3_KEY}" >&2
        exit 2
        ;;
esac

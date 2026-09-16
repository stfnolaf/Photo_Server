FROM postgres:17-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends awscli ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backup.sh /usr/local/bin/photo-postgres-backup
ENTRYPOINT ["photo-postgres-backup"]
CMD ["loop"]

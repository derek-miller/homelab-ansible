FROM postgres:16

# Fail the build if Debian has moved past the pg_cron version the image tag claims.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-cron \
    && dpkg-query -W -f='${Version}' postgresql-16-cron | grep -qE '^1\.6\.7[-.]' \
    && rm -rf /var/lib/apt/lists/*

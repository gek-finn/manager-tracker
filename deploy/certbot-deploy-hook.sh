#!/bin/sh
set -eu
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/manager.gilinsky.ru ]; then
    /usr/sbin/nginx -t
    /usr/bin/systemctl reload nginx
fi

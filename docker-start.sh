#!/bin/sh
set -eu

# Static files are generated inside the image/container at startup. Database
# migrations are deliberately not run here: preview deployments must never
# modify the production PostGIS database.
python manage.py collectstatic --noinput
exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-80}" --workers 2 --timeout 60

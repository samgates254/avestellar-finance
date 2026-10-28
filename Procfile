# AVESTELLAR FINANCE — process definitions for a Procfile-based PaaS.
# The app must never run Django's development server in staging/production.
# Web: Gunicorn WSGI server. Set WEB_CONCURRENCY/workers via your platform.
web: gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-3} --timeout 60 --access-logfile - --error-logfile -
# Background: drains the transactional outbox / webhook deliveries. The
# process_outbox command is a one-shot drain, so it is polled in a loop here.
# Run as a separate always-on process (or a scheduled/cron worker) in staging.
worker: sh -c 'while true; do python manage.py process_outbox; sleep 15; done'

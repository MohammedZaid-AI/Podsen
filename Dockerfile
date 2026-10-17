FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FLASK_ENV=production PORT=3000 DATA_DIR=/tmp/podsen-data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Run as an unprivileged user. User data and all caches are in Supabase Postgres;
# DATA_DIR only holds /label drafts, so a temporary folder is enough.
RUN useradd --create-home --uid 10001 podsen && mkdir -p /tmp/podsen-data \
    && chown podsen /tmp/podsen-data
USER podsen

EXPOSE 3000
# 2 workers. --timeout 300 (>= 120): a first "today" run fetches every saved feed while
# the user waits. No access log on purpose: query strings such as /admin?key= would
# otherwise end up in the logs. Render sets $PORT.
CMD ["sh", "-c", "exec gunicorn app:app --workers 2 --timeout 300 --bind 0.0.0.0:${PORT}"]

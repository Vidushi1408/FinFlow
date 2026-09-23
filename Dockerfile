# Same minor version CI runs the test suite on, so the image runs what was tested.
FROM python:3.12-slim

WORKDIR /app

# libpq-dev/gcc for pandas/psycopg2; postgresql-client gives this image its own pg_dump/pg_restore
# so `docker compose run --rm webapp python scripts/backup_db.py` works with no host tools required.
RUN apt-get update && apt-get install -y libpq-dev gcc postgresql-client && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# By default, run the Flask webapp using gunicorn
ENV PYTHONPATH=/app
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "webapp.app:app"]

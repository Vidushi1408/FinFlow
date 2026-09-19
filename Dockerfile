# Same minor version CI runs the test suite on, so the image runs what was tested.
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for pandas/psycopg2 if needed
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# By default, run the Flask webapp using gunicorn
ENV PYTHONPATH=/app
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "webapp.app:app"]

FROM python:3.12-slim AS base

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# `test` target: same app image plus the regression suite.  The suite also
# needs the demo paper site source; compose mounts it at /papersite.
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
ENV DATABASE_PATH=/tmp/test.db
CMD ["python", "-m", "pytest", "-q"]

# default/runtime target: the crawl API
FROM base AS runtime
ENV DATABASE_PATH=/data/crawler.db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

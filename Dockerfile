# One image for all of our Python services; each compose service picks its command.
FROM python:3.14.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the locked dependencies first, so code edits don't re-download everything.
COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app /data
USER app

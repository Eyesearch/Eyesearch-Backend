FROM python:3.12-slim

# Unbuffered output so `docker logs` shows the crawl as it happens, and no
# .pyc files in a read-only-ish container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] carries a prebuilt libpq, so the image needs no build
# toolchain and no apt packages.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Run as a normal user: nothing here needs root, and a crawler parses untrusted
# HTML, so the blast radius of a parser bug should not be uid 0.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# The app answers /health with db status, so a failing database shows up as an
# unhealthy container rather than a silently idle crawler.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

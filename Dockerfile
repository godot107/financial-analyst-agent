# The HTTP service (iteration 3). Phase B deploys this same image to Lambda, so
# what was tested here is what runs there.
FROM python:3.12-slim

# No .pyc litter; logs straight to stdout for whatever collects them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY fin_analyst ./fin_analyst

# Never root: the service holds API keys and spends money.
RUN useradd --create-home analyst && mkdir -p /app/runs && chown analyst /app/runs
USER analyst

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health')"

# Secrets arrive as environment variables at run time, never in the image:
#   ANTHROPIC_API_KEY, SEC_USER_AGENT, FIN_ANALYST_API_KEYS, ALPHAVANTAGE_KEY
CMD ["python", "-m", "fin_analyst.server"]

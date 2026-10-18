FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system surge \
    && useradd --system --gid surge --home-dir /app surge

COPY --chown=surge:surge dashboard ./dashboard
COPY --chown=surge:surge bot ./bot
COPY --chown=surge:surge data/collectors ./data/collectors
COPY --chown=surge:surge data/history.py data/backtesting.py data/call_log.py data/public_metrics.py ./data/
COPY --chown=surge:surge research/autonomous ./research/autonomous
COPY --chown=surge:surge prediction ./prediction
COPY --chown=surge:surge strategies ./strategies
RUN mkdir -p /app/data /var/lib/surge && chown -R surge:surge /app/data /var/lib/surge

USER surge
EXPOSE 8000
CMD ["uvicorn", "dashboard.backend.app:app", "--host", "0.0.0.0", "--port", "8000"]

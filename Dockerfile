FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LUCYA_DATA=/data

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && mkdir /data
COPY aio/ ./aio/

# Root, on purpose and without pretence: whoever holds the Docker socket is
# root on the host anyway. What this container may do is limited by what
# its API accepts (aio/templates.py), not by its user.
EXPOSE 8061
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8061/health', timeout=3)"
ENTRYPOINT ["python", "-m", "aio"]

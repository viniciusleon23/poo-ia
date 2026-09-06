FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 pooia \
    && useradd --uid 10001 --gid pooia --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin pooia \
    && install -d -o pooia -g pooia -m 0700 /app/data

COPY --chown=pooia:pooia app ./app
COPY --chown=pooia:pooia rules ./rules
COPY --chown=pooia:pooia personality ./personality

USER 10001:10001

CMD ["python", "-m", "app"]

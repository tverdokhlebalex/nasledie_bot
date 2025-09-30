# (опционально) в самом верху:
# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# системные утилиты и tzdata (без лишних dev-пакетов)
#RUN apt-get update && apt-get install -y --no-install-recommends \
#    tzdata \
#&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
# wheelhouse может быть пуст/неполон → делаем фолбэк на PyPI
COPY wheelhouse/ /opt/wheels/

RUN python -m pip install -U pip && \
    if [ -d /opt/wheels ] && [ "$(ls -A /opt/wheels || true)" ]; then \
        echo "Installing from wheelhouse (offline)..." && \
        python -m pip install --no-index --find-links /opt/wheels -r /tmp/requirements.txt || \
        (echo "Wheelhouse incomplete, falling back to PyPI..." && python -m pip install --no-cache-dir -r /tmp/requirements.txt); \
    else \
        echo "Installing from PyPI..." && \
        python -m pip install --no-cache-dir -r /tmp/requirements.txt; \
    fi

COPY . .

# ваш CMD/ENTRYPOINT как было
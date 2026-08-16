FROM python:3.12-slim

ARG XRAY_VERSION=26.7.28

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    XRAY_BINARY=/usr/local/bin/xray

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
    && curl -fsSL \
        -o /tmp/xray.zip \
        "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip" \
    && unzip -q -p /tmp/xray.zip xray > /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray \
    && /usr/local/bin/xray version \
    && rm -f /tmp/xray.zip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]

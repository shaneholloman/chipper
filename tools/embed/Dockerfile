FROM python:3.12-slim

ARG VERSION=latest
ENV APP_VERSION=$VERSION

ARG BUILD_NUM=0
ENV APP_BUILD_NUM=$BUILD_NUM

WORKDIR /app

RUN apt-get update && apt-get install -y gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# First copy example, and then use user ragignore if it can be found
COPY .ragignore.example .ragignore
COPY .ragignor[e] .ragignore

RUN mkdir -p /app/data

ENTRYPOINT ["python", "src/main.py"]
CMD []

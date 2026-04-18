FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev npm nodejs bash coreutils \
    && npm install -g @google/gemini-cli@0.36.0
RUN apk add --no-cache ffmpeg

COPY agents/tg_transfer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.tg_transfer"]

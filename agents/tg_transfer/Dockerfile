FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir aiosqlite>=0.20 imagehash>=4.3 Pillow>=10

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.tg_transfer"]

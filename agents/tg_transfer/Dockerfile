FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir aiosqlite>=0.20

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.tg_transfer"]

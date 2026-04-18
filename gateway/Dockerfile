FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev

COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY gateway/ ./gateway/

CMD ["python", "-m", "gateway"]

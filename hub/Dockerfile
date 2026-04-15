FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY hub/ ./hub/
COPY config.yaml .
COPY run_hub.py .

EXPOSE 9000

CMD ["python", "run_hub.py"]

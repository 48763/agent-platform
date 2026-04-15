FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev npm nodejs git bash
RUN npm install -g @anthropic-ai/claude-code@2.1.92

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY agents/ ./agents/

CMD ["python", "-m", "agents.claude_code"]

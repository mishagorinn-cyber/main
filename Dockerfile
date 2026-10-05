FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY connector ./connector
RUN pip install --no-cache-dir .

ENV CONNECTOR_CONFIG=/app/config.yaml
VOLUME ["/app/data"]
EXPOSE 8080
CMD ["python", "-m", "connector", "serve"]

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY sessionbot ./sessionbot
RUN pip install --no-cache-dir .
CMD ["python", "-m", "sessionbot"]

FROM python:3.12-slim
WORKDIR /app
ARG DEV=0
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r $([ "$DEV" = "1" ] && echo requirements-dev.txt || echo requirements.txt)
COPY app app
ENV DATA_DIR=/app/data
VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

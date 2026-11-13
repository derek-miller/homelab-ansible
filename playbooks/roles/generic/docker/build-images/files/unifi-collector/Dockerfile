FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

WORKDIR /app
COPY collector.py .

USER nobody

CMD ["python", "-u", "collector.py"]

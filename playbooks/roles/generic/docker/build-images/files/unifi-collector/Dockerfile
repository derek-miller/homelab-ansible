FROM python:3.12-slim

RUN pip install --no-cache-dir 'requests>=2.31,<3'

WORKDIR /app
COPY collector.py .

USER nobody

CMD ["python", "-u", "collector.py"]

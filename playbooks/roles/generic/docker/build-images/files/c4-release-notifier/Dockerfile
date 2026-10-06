FROM python:3.12-slim

RUN pip install --no-cache-dir 'requests>=2.31,<3'

WORKDIR /app
COPY notifier.py .

RUN mkdir /data && chown nobody /data
VOLUME /data

USER nobody

CMD ["python", "-u", "notifier.py"]

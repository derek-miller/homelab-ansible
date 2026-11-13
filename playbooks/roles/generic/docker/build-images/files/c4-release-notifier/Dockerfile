FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

WORKDIR /app
COPY notifier.py .

RUN mkdir /data && chown nobody /data
VOLUME /data

USER nobody

CMD ["python", "-u", "notifier.py"]

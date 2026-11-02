FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY triage.py harness.py samples.jsonl sms.py sms_client.py ./
# Runs as an unprivileged user; /state is the only place it writes
RUN useradd --uid 3100 --no-create-home triage && mkdir /state && chown triage /state
USER triage
CMD ["python", "triage.py"]

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY panel/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY panel/app ./app

ENV MRMPANEL_DATA=/var/lib/mrmpanel \
    MRMPANEL_STACKS=/opt/mrmpanel/stacks \
    MRMPANEL_ASSETS=/opt/mrmpanel/assets \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim

# Install Chromium (for Kaleido v1+) and Tini
# Chromium package automatically installs all required dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /usr/src/namazu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV KALEIDO_CHROME_PATH=/usr/bin/chromium

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "main.py"]
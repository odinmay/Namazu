FROM python:3.12-slim

# Install Chromium and minimal dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /usr/src/namazu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tell Kaleido to use this Chromium
ENV KALEIDO_CHROME_PATH=/usr/bin/chromium

CMD ["python", "-u", "main.py"]

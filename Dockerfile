FROM python:3.12-alpine
LABEL authors="odin"

# Install runtime dependencies for common Python packages
RUN apk add --no-cache libffi openssl

# Create working directory
WORKDIR /usr/src/namazu

# Copy only requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies (use --no-cache-dir to reduce image size)
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the application
COPY . .

# Run the bot
CMD ["python", "-u", "main.py"]

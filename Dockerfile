FROM python:3.12-slim
RUN pip install --no-cache-dir tinytuya paho-mqtt
WORKDIR /app
COPY ep2500_bridge.py .
CMD ["python", "-u", "ep2500_bridge.py"]

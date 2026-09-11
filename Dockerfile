FROM python:3.12-slim
RUN pip install --no-cache-dir tinytuya==1.20.0 paho-mqtt==2.1.0
WORKDIR /app
COPY ep2500_bridge.py .
CMD ["python", "-u", "ep2500_bridge.py"]

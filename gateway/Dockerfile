# 体脂秤 BLE 网关（NAS 上与 app 同机部署，见 docker-compose.miscale.yml）
FROM python:3.12-slim

# bleak 走宿主机 BlueZ 的 D-Bus 接口，容器内不需要蓝牙驱动，但需要 dbus 客户端库
RUN pip install --no-cache-dir bleak==0.22.3

WORKDIR /srv/gateway
COPY miscale_listener.py .

CMD ["python", "miscale_listener.py"]

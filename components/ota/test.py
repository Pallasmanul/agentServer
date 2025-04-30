
import paho.mqtt.client as mqtt
import ssl
import os


# EMQX 服务器配置
MQTT_BROKER = "192.168.0.111"
MQTT_PORT = 8883  # SSL 端口
CLIENT_ID = "test_client"

# 获取当前脚本所在目录
current_dir = os.path.dirname(os.path.abspath(__file__))

# TLS 证书配置
CA_CERT_PATH = os.path.join(current_dir, "storage/tls/rootCA.pem")  # 根证书路径
CLIENT_CERT_PATH = os.path.join(current_dir, "storage/tls/client.crt")  # 客户端证书路径
CLIENT_KEY_PATH = os.path.join(current_dir, "storage/tls/client.key")  # 客户端密钥路径


# 连接成功回调
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected successfully")
        # 连接成功后订阅主题
        client.subscribe("test/topic")
    else:
        print(f"Connect failed with code {rc}")

# 收到消息回调
def on_message(client, userdata, msg):
    print(f"Received message on topic '{msg.topic}': {msg.payload.decode()}")

# 创建 MQTT 客户端
client = mqtt.Client(client_id=CLIENT_ID)

# 设置 TLS 配置
client.tls_set(
    ca_certs=CA_CERT_PATH,
    certfile=CLIENT_CERT_PATH,
    keyfile=CLIENT_KEY_PATH,
    cert_reqs=ssl.CERT_REQUIRED,
    tls_version=ssl.PROTOCOL_TLSv1_2
)

# 设置回调函数
client.on_connect = on_connect
client.on_message = on_message

# 连接到 EMQX 服务器
client.connect(MQTT_BROKER, MQTT_PORT, 60)

# 开始循环处理网络流量
client.loop_forever()

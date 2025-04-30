import json
import uuid
import logging
import os
import asyncio
from contextlib import asynccontextmanager

from asyncio_mqtt import Client, MqttError, TLSParameters

from fastapi import FastAPI, Request, Response
import redis

import aiohttp
import socket
import ssl

# 配置日志记录
current_dir = os.getcwd()
# 拼接日志文件路径
log_file_path = os.path.join(current_dir, "storage/logs/ota.log")
logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


## Configure 
CONFIG_LATEST_FIRMWARE_VERSION = "1.5.5"



#####################
#  REPONSE
#####################


# error response
OTA_ERROR_REPONSE = {"status": "error", "message": "OTA update failed"}
# activate response
OTA_ACTIVATE_REPONSE = {"message": "请使用以下激活码激活设备", "code": "******"}
# mqtt response
OTA_MQTT_REPONSE = {"endpoint": "mqtt.example.com", "client_id": "**** *** *** ****", "username": "", "password": "", "publish_topic": "device/001/status"}
# server time response
OTA_SERVER_TIME_REPONSE = {"timestamp": 1629103600, "timezone_offset": 480}
# firmware response
OTA_FIRMWARE_REPONSE = {"version": "1.5.5", "url": "http://192.168.0.111:8000/firmware"}


#####################
# redis数据库
#####################
redis_client = redis.Redis(host="localhost", port=6379, db=0)
redis_pubsub = redis_client.pubsub()

# 订阅的频道
REDIS_CHANNEL_NAME = "ota_channel"
redis_pubsub.subscribe("ota_channel")


# 存储redis 监听任务句柄
redis_listen_task = None

# 定义订阅的频道
async def listen_redis():
    while True:
        message = redis_pubsub.get_message()
        if message and message["type"] == "message":
            print(f"Received message: {message['data']}")
        await asyncio.sleep(0.1)


# 应用关闭时停止 Redis 监听



#####################
# OTA升级
#####################


# 检查固件版本
def check_firmware_version(version):
    print(OTA_FIRMWARE_REPONSE["version"])
    print(version)

    # 将版本号字符串拆分为数字列表
    def split_version(ver):
        return list(map(int, ver.split(".")))

    current_version = split_version(OTA_FIRMWARE_REPONSE["version"])
    target_version = split_version(version)

    # 依次比较版本号的每个部分
    for i in range(max(len(current_version), len(target_version))):
        current_part = current_version[i] if i < len(current_version) else 0
        target_part = target_version[i] if i < len(target_version) else 0
        if current_part > target_part:
            return True
        elif current_part < target_part:
            return False
    return False


def pack_response(res_activate, res_mqtt, res_servertime, res_firmware):
    data = {
        "activate": res_activate,
        "mqtt": res_mqtt,
        "servertime": res_servertime,
        "firmware": res_firmware,
    }
    return data


def process_ota(data):
    reponse = {}  # 存储响应数据
    reponse["firmware"] = OTA_FIRMWARE_REPONSE  # 存放固件信息

    return reponse





#####################
#   API 请求
#####################


async def call_audio_io_create_udp(session_id: str, udp_address: str):
    audio_io_api_url = "http://localhost:8001/create_udp_channel"
    payload = {
        "session_id": session_id,
        "udp_address": udp_address,
        "input_sample_rate": 16000, # 输入采样率
        "channels": 1,
        "frame_duration": 30
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(audio_io_api_url, json=payload) as response:
                if response.statue == 200:
                    result = await response.json()
                    print(f"Response from audio_io: {result}")
                    return result
                else:
                    print(f"Failed to call audio_io API. Status code: {response.status}")
                    return None
        except Exception as e:
            print(f"Error calling audio_io API: {e}")
            return None



#####################
#   Util
#####################

def get_unused_udp_port():
    # 创建一个 UDP 套接字
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 绑定到本地地址， 端口号设为 0 表示让系统分配一个未使用的端口
        sock.bind(('localhost', 0))
        # 获取分配的端口号
        _, port = sock.getsockname()
        return port
    finally:
        # 关闭套接字
        sock.close()




#####################
#   MQTT
#####################


MQTT_BROKER = "192.168.0.111"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
MQTT_USERNAME = "your_username"  # 替换为实际的用户名
MQTT_PASSWORD = "your_password"  # 替换为实际的密码


# 配置 TLS 参数
tls_params = TLSParameters(
    ca_certs=None,     # 替换为 CA 证书文件路径， 如果服务器使用自签名证书需要配置
    certfile=None,     # 客户端证书文件路径
    keyfile=None,      # 客户端私钥文件路径
    cert_reqs=ssl.CERT_REQUIRED,
    tls_version=ssl.PROTOCOL_TLSv1_2,
    ciphers=None
)


# 异步 MQTT 客户端类

class AsyncMQTTClient:
    def __init__(self, client_id):
        self.client_id = client_id
        self.client = Client(
            MQTT_BROKER,
            MQTT_PORT,
            client_id=client_id,
            clean_session=False,
            username=None,
            password=None,
            tls_params=tls_params
        )
        
    async def connect(self):
        try:
            await self.client.connect()
            print(f"MQTT client {self.client_id} connected successfully")
        except MqttError as e:
            print(f"MQTT client {self.client_id} failed to connect: {e}")
            
    async def publish(self, topic, payload)        :
        try:
            await self.client.publish(topic, payload.encode())
        except MqttError as e:
            print(f"Failed to publish message to {topic}: {e}")
            
    async def subscribe(self, topic):
        try:
            async with self.client.filtered_messages(topic) as messages:
                await self.client.subscribe(topic)
                print(f"Subscribed to topic '{topic}' successfully")
                async for message in messages:
                    self.on_message(message.topic, message.payload.decode())
        except MqttError as e:
            print(f"Failed to subscribe to topic '{topic}: 'e''")
            
    
    def on_message(self, topic, payload):
        print(f"Received message on topic {topic}: {payload}")
        try:
            # 解析收到的 JSON 数据
            message = json.laods(payload)
            print(f"Parsed JSON data: {message}")
            
            # 处理 hello 消息
            # 初始化 session_id, udp 通道 , 并将会话信息保存到 redis 数据库中
            if message.get("type") == "hello":
                # 解析 audio_params
                audio_params = message.get("audio_params")
                if audio_params:
                    audio_format = audio_params.get("format")
                    sample_rate = audio_params.get("sample_rate")
                    channels = audio_params.get("channels")
                    frame_duration = audio_params.get("frame_duration")
                    
            client_ip_map = {}
            client_id = self.client_id
            client_ip = client_ip_map.get(client_id)
            print(client_ip)

            # 获取未使用的本地端口号， 用于创建UDP地址
            unused_port = get_unused_udp_port()
            udp_address = ('localhost', unused_port)
            
            loop = asyncio.get_event_loop()
            session_id = str(uuid.uuid4())
            loop.create_task(call_audio_io_create_udp(session_id, udp_address))
            
            print(f"Generated session_id: {session_id}")
            
            # 将 session_id 存放到集合中
            redis_client.sadd("session_list", session_id)
            
            # 创建一个 session_id 哈希
            redis_client.hset(
                session_id,
                mapping={
                    "tts_role": "saike",
                    "llm_input": "",
                    "tts_output": "",
                    "audio": "",
                    "ip_address":""
                },
            )
        except Exception as e:
            print(f"Error processing message: {e}")
            
    

    async def disconnect(self):
        try:
            await self.client.disconnect()
            print(f"MQTT client {self.client_id} disconnected successfully")
        except MqttError as e:
            print(f"Failed to disconnect MQTT client {self.client_id}: {e}")



# 存储mqtt客户端实例
mqtt_clients = {}


def mqtt_client_create(client_id, device_id):
    if client_id in mqtt_clients:
        print(f"MQTT client for {client_id} already exists.")
        return mqtt_clients[client_id], None

    mqtt_client = AsyncMQTTClient(client_id)
    asyncio.create_task(mqtt_client.connect())

    subscribe_topic = None
    if device_id:
        subscribe_topic = f"device"
        asyncio.create_task(mqtt_client.subscribe(subscribe_topic))

    mqtt_clients[client_id] = mqtt_client
    return mqtt_client, subscribe_topic


def mqtt_client_delete(client_id):
    if client_id in mqtt_clients:
        mqtt_client = mqtt_clients[client_id]
        asyncio.create_task(mqtt_client.disconnect())
        del mqtt_clients[client_id]
        print(f"MQTT client for {client_id} has been deleted.")
    else:
        print(f"No MQTT client found for {client_id}")



#####################
# FastTAPI 部分
#####################

# lifespan 上下文管理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时开始 Redis 监听
    global redis_listen_task
    redis_listen_task = asyncio.create_task(listen_redis())
    yield
    # 应用关闭时停止 Redis 监听
    redis_listen_task.cancel()
    try:
        await redis_listen_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


# 处理根路径的GET和POST请求
@app.api_route("/", methods=["GET", "POST"])
async def handle_root(request: Request):
    if request.method == "GET":
        # 暂不处理， 应该不会使用GET请求
        logging.info(f"Received GET request at root, but not should be, we pass here")
        pass

    elif request.method == "POST":
        content_type = request.headers.get("Content-Type")
        print("Request Headers:", dict(request.headers))

        reponse = {}  # 存储响应数据
        if content_type == "application/json":
            try:
                
                #    解析数据    #
                data = await request.json()


                client_id = request.headers.get("client-id")
                device_id = request.headers.get("device-id")
                firmware_version = data['application']['version']
    

                #    创建资源      #
                OTA_FIRMWARE_REPONSE['version'] = CONFIG_LATEST_FIRMWARE_VERSION
                reponse["firmware"] = OTA_FIRMWARE_REPONSE  # 存放固件信息
                                
                if client_id:
                    # 创建 MQTT 客户端
                    print("create mqtt client id")
                    mqtt_client, subscribe_topic = mqtt_client_create(client_id, device_id)


                OTA_MQTT_REPONSE["endpoint"] = MQTT_BROKER
                OTA_MQTT_REPONSE["client_id"] = client_id 
                OTA_MQTT_REPONSE["username"] = ""  
                OTA_MQTT_REPONSE["password"] = ""
                OTA_MQTT_REPONSE["publish_topic"] = subscribe_topic
                reponse["mqtt"] = OTA_MQTT_REPONSE  

                
                #    处理返回帧    #
                # reponse["servertime"] = OTA_SERVER_TIME_REPONSE  # 存放服务器时间信息
                # reponse["activate"] = OTA_ACTIVATE_REPONSE  # 存放激活码信息



            except json.JSONDecodeError as e:
                logging.error(f"Error parsing JSON data at root: {str(e)}")

        return reponse


@app.api_route("/firmware", methods=["GET", "POST"])
async def firmware():
    firmware_file_path = os.path.join(current_dir, "storage/firmware/xiaozhi.bin")
    print(firmware_file_path)
    print("start send firmware file")

    try:
        with open(firmware_file_path, "rb") as file:
            content = file.read()

        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=example.hex"},
        )

        logging.info(f"Hex file {log_file_path} not found.")
    except FileNotFoundError:
        print(f"Firmware file not found at {firmware_file_path}")
        return {"error": "Firmware file not found"}
    except Exception as e:
        print(f"Error reading firmware file: {str(e)}")
        return {"error": "Error reading firmware file"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="192.168.0.111", port=8000)

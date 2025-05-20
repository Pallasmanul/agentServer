import os
import sys
import asyncio
import aiomqtt
from aiomqtt import MqttError
from typing import Annotated
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
import ssl
import json
import logging
import aiohttp
import uuid
from pydantic import BaseModel

# 在 Windows 系统上设置 SelectorEventLoop ， 在Windows 下比武配置 ， 不然创建MQTT连接会出错

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


#######################################################################
#    配置日志
#######################################################################

# 配置日志记录

logger = logging.getLogger("manager")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "manager.log")

# 创建文件处理器
file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

# 创建控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

# 添加处理器
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 关闭日志（需要时取消注释）
# logger.setLevel(logging.CRITICAL)


#######################################################################
#    模块配置
#######################################################################

# MQTT 服务器配置
UVICORN_HOST = "192.168.0.111"  # FastAPI服务监听地址
UVICORN_PORT = 8006  # FastAPI服务监听端口


MQTT_BROKER = "192.168.0.111"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
MQTT_USERNAME = "your_username"  # 替换为实际的用户名
MQTT_PASSWORD = "your_password"  # 替换为实际的密码
CLIENT_ID = "lienji"
SUBSCRIBE_TOPIC = "device/#"

# MQTT 客户端
client = None


async def get_mqtt():
    yield client


# CA 证书路径
current_dir = os.path.dirname(os.path.abspath(__file__))
ca_cert_file = os.path.join(current_dir, "storage/tls/rootCA.pem")

# 配置 TLS 参数
tls_params = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
tls_params.load_verify_locations(ca_cert_file)  # 加载 CA 证书
tls_params.verify_mode = ssl.CERT_REQUIRED


# 配置 URL 地址
PUBLIC_IP_ADDRESS = "192.168.0.111"

AUDIO_IO_BASE_URL = "http://192.168.0.111:8001"
AUDIO_IO_URL = "http://192.168.0.111:8001/udp_pool"
DAO_DEVICE_URL = "http://192.168.0.111:8005/devices"
DAO_SESSIONS_URL = "http://192.168.0.111:8005/sessions"


EMQX_API_URL = "http://192.168.0.111:18083/api/v5"
USERNAME = "admin"
PASSWORD = "@lianjikeji"


# 全局字典：存储 client_id 与订阅地址的映射（格式：{client_id: subscribe_topic}）
client_subscriptions = {}

#######################################################################
#    API函数
#######################################################################


async def emqx_set_client_subscribe(client_id, topic, qos=0):
    url = f"{EMQX_API_URL}/clients/{client_id}/subscriptions/_batch"
    payload = {"topic": topic, "qos": qos}

    try:
        async with aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(USERNAME, PASSWORD)
        ) as session:
            async with session.post(f"{url}", json=payload) as response:
                if response.status == 200:
                    logger.info(f"成为客户端 {client_id} 订阅主题 {topic}")
                    return await response.json()
                else:
                    logger.error(
                        f"订阅失败: 状态码  {response.status}, 错误消息 {await response.text()}"
                    )

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")


async def emqx_del_client_subscribe(client_id, topic, qos=0):
    """
    通过 EMQX API 取消客户端订阅指定主题
    参数:
    """
    url = f"{EMQX_API_URL}/clients/{client_id}/subscriptions"
    payload = {"topic": topic, "qos": qos}  # 与设置订阅的 payload 结构一致

    try:
        async with aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(USERNAME, PASSWORD)
        ) as session:
            # 使用 DELETE 方法发送请求
            async with session.delete(f"{url}", json=payload) as response:
                if response.status == 200:
                    logger.info(f"客户端 {client_id} 取消订阅主题 {topic} 成功")
                    return await response.json()
                else:
                    logger.error(
                        f"取消订阅失败: 状态码 {response.status}, 错误消息 {await response.text()}"
                    )

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")

    return None


async def create_udp_audio_channel(
    session_id: str, input_sample_rate, channels, frame_duration
) -> bool:
    """调用 audio_io 接口创建udp音频通道"""
    global AUDIO_IO_URL

    try:
        async with aiohttp.ClientSession() as session:
            request = {
                "session_id": session_id,
                "input_sample_rate": input_sample_rate,
                "channels": channels,
                "frame_duration": frame_duration,
            }

            async with session.post(
                f"{AUDIO_IO_BASE_URL}/udp_channel", json=request
            ) as response:
                if response.status == 200:
                    logger.info(f"udp 音频通道创建成功")
                    data = await response.json()
                    return data
                logger.error(f"udp 音频通道创建失败: {await response.text()}")
                return {}

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return {}

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return {}


async def get_device_by_client(client_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DAO_DEVICE_URL}/client", params={"client_id": client_id}
            ) as response:
                if response.status == 200:
                    logger.info(f"device by client 查询成功")
                    data = await response.json()
                    return data
                logger.error(f"device by client 查询失败: {await response.text()}")
    except Exception as e:
        logger.error(f"device by client 查询异常 : {str(e)}")


async def dao_create_session(session_data: dict) -> bool:
    """创建SESSION"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DAO_SESSIONS_URL, json=session_data) as response:
                if response.status == 200:
                    logger.info(f"会话创建成功 session:{session_data['session_id']}")
                    return True
                logger.error(f"DAO接口返回错误: {await response.text()}")
                return False
    except aiohttp.ClientError as e:
        logger.error(f"DAO服务连接失败: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"会话创建失败")
        return False


async def dal_delete_session(session_id: str) -> bool:
    """
    通过 DAO 接口删除指定 session
    参数:
        session_id (str): 要删除的会话ID
    返回:
        bool: 删除成功返回 True , 失败返回False
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(f"{DAO_SESSIONS_URL}/{session_id}") as response:
                if response.status == 200:
                    logger.info(f"session {session_id} 删除成功")
                    return True
                else:
                    logger.error(
                        f"session {session_id} 删除失败, 状态码: {response.status}, 错误: {await response.text()}"
                    )
                    return False
    except aiohttp.ClientError as e:
        logger.error(f"调用DAO接口网络异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"删除session异常: {str(e)}")
        return False


async def get_client_id_by_session_id(session_id: str) -> str:
    """通过 session_id 查询对应的 client_id"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{DAO_SESSIONS_URL}/{session_id}") as response:
                if response.status == 200:
                    logger.info("client_id 查询成功")
                    data = await response.json()
                    return data.get("client_id", "")
                logger.error(f"client_id 查询失败 : {await response.text()}")
    except Exception as e:
        logger.error(f"client_id 查询异常 : {str(e)}")


async def get_device_data(device_id: str, field: str) -> str:
    """
    通过 DAO 接口获取设备指定字段的数据
    参数:
        device_id(str): 设备ID
        field (str): 需要获取的字段名 (如"client_id", "username", "ip_address")
    返回:
        str: 字段对应的值
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 调用 DAO 的设备详情接口 (返回完整设备信息)
            async with session.get(
                f"{DAO_DEVICE_URL}", params={"device_id": device_id}
            ) as response:
                if response.status != 200:
                    logger.error(f"获取设备 {device_id} 信息失败, 状态码 : {response}")
                    return ""

                device_data = await response.json()
                field_value = device_data.get(field, "")
                if not field_value:
                    logger.warning(f"设备 {device_id} 中字段 {field} 不存在或为空")

                return field_value

    except aiohttp.ClientError as e:
        logger.error(f"调用 DAO 接口网络异常: {str(e)}")
        return ""

    except json.JSONDecodeError:
        logger.error(f"设备 {device_id} 信息解析失败 (非JSON格式)")
        return ""

    except Exception as e:
        logger.error(f"获取设备字段 {field} 异常: {str(e)}")
        return ""


async def refresh_session_expired(session_id) -> bool:
    """
    通过 DAO 接口刷新session_id的会话过期时间
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{DAO_SESSIONS_URL}/refresh"
            response = await session.post(url, params={"session_id": session_id})
            if response.status == 200:
                logger.info(f"成功刷新会话 , session_id: {session_id}")
                return True
            return False

    except aiohttp.ClientError as e:
        logger.error(f"调用 DAO 接口网络异常: {str(e)}")
        return ""

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return ""


async def check_session_exists(session_id: str) -> bool:
    """
    通过 DAO 接口检查指定session是否存在
    参数:
        session_id (str): 要检查的会话ID
    返回:
        bool: 存在返回True, 不存在返回False
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 调用 DAO 的session详情接口 (若存在返回200, 不存在返回404)
            async with session.get(f"{DAO_SESSIONS_URL}/{session_id}") as response:
                if response.status == 200:
                    logger.info(f"Session {session_id} 存在")
                    return True
                elif response.status == 404:
                    logger.info(f"Session {session_id} 不存在")
                    return False
                else:
                    error_msg = await response.text()
                    logger.error(
                        f"检查session存在性失败, 状态码: {response.status}, 错误: {error_msg}"
                    )
                    return False
    except aiohttp.ClientError as e:
        logger.error(f"调用DAO接口网络异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"检查session存在性错误: {str(e)}")
        return False


async def update_device_field(device_id: str, field: str, value: str) -> bool:
    """
    通过 DAO 接口更新设备指定字段的值
    参数:
        device_id (str) : 设备ID
        field (str) : 需要更新的字段名 (如 "client_id", "username", "ip_address", "session_id")
        value (str) : 字段新值
    返回:
        bool: 更新成功返回 True , 失败返回 False

    示例:
    # 在其他逻辑中调用（例如处理设备信息变更时）
    success = await update_device_field(
        device_id="cc:ba:97:04:92:94",
        field="username",
        value="新用户名"
    )
    if success:
        logger.info("设备用户名更新成功")
    else:
        logger.error("设备用户名更新失败")
    """
    try:
        # 1. 获取设备当前完整数据
        async with aiohttp.ClientSession() as session:
            # 调用 DAO 的设备详情接口获取完整数据
            async with session.get(
                f"{DAO_DEVICE_URL}", params={"device_id": device_id}
            ) as get_response:
                if get_response.status != 200:
                    logger.error(
                        f"获取设备 {device_id} 数据失败, 状态码: {get_response.status}"
                    )
                    return False
                current_device = await get_response.json()
                if not current_device:
                    logger.error(f"设备 {device_id} 不存在")
                    return False

            # 2. 检查字段是否存在更新
            if field not in current_device:
                logger.error(f"设备 {device_id} 不存在字段: {field}")
                return False

            current_device[field] = value

            # 调用 DAO 的设备更新接口
            async with session.put(
                f"{DAO_DEVICE_URL}", json=current_device
            ) as update_response:
                if update_response.status == 200:
                    logger.info(f"设备 {device_id} 字段 {field} 更新成功")
                    return True
                else:
                    logger.error(
                        f"设备 {device_id} 字段 {field} 更新失败: {await update_response.text()}"
                    )

    except aiohttp.ClientError as e:
        logger.error(f"调用 DAO 接口网络异常: {str(e)}")
        return False

    except json.JSONDecodeError:
        logger.error(f"设备 {device_id} 数据解析失败 (非JSON格式)")
        return False

    except Exception as e:
        logger.error(f"更新设备字段异常: {str(e)}")
        return False


async def get_session_data(
    session_id: str,
) -> dict:
    """
    通过 DAO 接口获取完整session数据
    参数:
        session_id (str): 要查询的会话ID
    返回:
        dict: 包含session详细信息的字典 (如 client_id, username等), 失败返回空字典
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 调用 DAO 的session详情接口
            async with session.get(f"{DAO_SESSIONS_URL}/{session_id}") as response:
                if response.status == 200:
                    logger.info(f"获取session {session_id} 数据成功")
                    return await response.json()
                logger.error(
                    f"获取session {session_id} 数据失败, 状态码: {response.status}, 错误: {await response.text()}"
                )
                return {}

    except aiohttp.ClientError as e:
        logger.error(f"调用DAO接口网络异常: {str(e)}")
        return {}

    except json.JSONDecodeError:
        logger.error(f"session {session_id} 数据解析失败 (非JSON格式)")
        return {}

    except Exception as e:
        logger.error(f"获取session数据异常: {str(e)}")
        return {}


async def get_udp_pool_info(session_id: str = None) -> dict:
    """
    通过 audio_io 模块接口获取 UDP 通道信息

    参数:
        session_id (str, optional): 可选的会话ID , 若提供则回去指定会话的 UDP 信息 ,  否则获取所有UDP信息

    返回:
        dict: UDP 池信息 (成功时) 或错误消息 (失败时)
    """

    try:
        # 构造请求 URL 和参数
        url = f"{AUDIO_IO_BASE_URL}/udp_pool"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"session_id": session_id}) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"成功获取 UDP 池信息: {data}")
                    return data
                else:
                    logger.error(
                        f"获取 UDP 池信息失败, 状态码: {response.status}, 响应内容: {await response.text()}"
                    )
                    return {}

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return {}
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return {}


#######################################################################
#    全局client_id 字典相关函数
#######################################################################
async def set_client_subscription(client_id: str, subscribe_topic: str = None) -> None:
    """
    设置客户端订阅地址 (仅在不存在时添加)
    参数:
        client_id (str): 客户端ID
        subscribe_topic (str, optional): 订阅主题 (默认格式为 "device_pub/{client_id}")
    """
    if client_id in client_subscriptions:
        logger.info(f"client_id {client_id} 已存在订阅记录, 跳过设置")
        return

    if not subscribe_topic:
        subscribe_topic = f"device_pub/{client_id}"

    subscribe_topic = "devices/p2p/cc_ba_97_04_92_94"

    client_subscriptions[client_id] = subscribe_topic
    logger.info(f"新增client_id订阅记录: {client_id} -> {subscribe_topic}")

    await emqx_set_client_subscribe(client_id, subscribe_topic, qos=0)


async def delete_client_subscription(client_id: str) -> None:
    """
    删除客户端订阅地址 (存在时删除)
    参数:
        client_id (str): 客户端ID
    """
    if client_id not in client_subscriptions:
        logger.warning(f"client_id {client_id} 无订阅记录, 无需删除")
        return

    # 先获取当前订阅主题
    subscribe_topic = client_subscriptions[client_id]

    # 调用 EMQX API 取消订阅 (QoS 使用默认值0)
    await emqx_del_client_subscribe(client_id, subscribe_topic, qos=0)

    # 再删除全局字典记录
    del client_subscriptions[client_id]
    logger.info(f"删除client_id订阅记录: {client_id}")


#######################################################################
#    消息发送函数
#######################################################################
# hello_msg = package_message(
#     "hello",
#     transport="udp",
#     session_id="<session_id>",  # 实际传入具体的session_id值
#     audio_params={
#         "sample_rate": 16000,
#         "frame_duration": 60,
#     },
#     udp={
#         "address": "<udp_address>",
#         "port": "<udp_port>",
#         "key": "<hex_key>",
#         "nonce": "hex_nonce",
#     }
# )
# goodbye_msg = package_message(
#     "goodbye",
#     session_id="session_123"
# )
# tts_msg = package_message(
#     "tts",
#     state="start",
#     text="即将播放的语音对应文本的内容"
# )
# stt_msg = package_message(
#     "stt",
#     text="用户语音转文字结果"
# )
# llm_msg = package_message(
#     "llm",
#     emotion="happy"
# )
# iot_msg = package_message(
#     "iot",
#     commands=[{
#         "name": "Speaker",
#         "method": "SetVolume",
#         "parameters": {"level": 50}
#     }]
# )


def package_message(msg_type: str, **kwargs) -> dict:
    """
    通用封包函数, 将参数转换为MQTT消息字典
    参数:
        msg_type: 消息类型 (如 hello, goodbye, iot)
        **kwargs: 消息扩展参数
    """
    base_msg = {
        "type": msg_type,
    }

    if msg_type == "hello":
        base_msg.update(
            {
                "type": "hello",
                "transport": kwargs.get("transport", "udp"),
                "session_id": kwargs.get("session_id", ""),
                "audio_params": kwargs.get("audio_params", {}),
                "udp": kwargs.get("udp", {}),
            }
        )

    elif msg_type == "goodbye":
        base_msg.update(
            {
                "session_id": kwargs.get("session_id"),
            }
        )

    elif msg_type == "tts":
        base_msg.update(
            {
                "type": "tts",
                "state": kwargs.get("state", ""),
                "text": kwargs.get("text"),
            }
        )

    elif msg_type == "stt":
        base_msg.update({"text": kwargs.get("text")})

    elif msg_type == "llm":
        base_msg.update({"emotion": kwargs.get("emotion")})

    elif msg_type == "iot":
        base_msg.update({"commands": kwargs.get("commands")})

    return base_msg


async def manager_send(
    client: Annotated[aiomqtt.Client, Depends(get_mqtt)], client_id: str, message: dict
):
    """
    通用消息发送函数
    参数:
        client: 获取MQTT客户端
        session_id: 会话ID(用于定位目标设备)
        message: 发送的消息内容
    """

    try:

        subscribe_topic = client_subscriptions.get(client_id)
        if not subscribe_topic:
            logger.error(f"client_id {client_id} 无法订阅地址记录, 无法发送消息")

        publish_topic = subscribe_topic

        await client.publish(publish_topic, json.dumps(message))
        logger.info(f"消息发送成功, 主题: {publish_topic}, 内容: {message}")
        return True

    except Exception as e:
        logger.error(f"消息发送失败, client_id={client_id}, 错误: {str(e)}")
        return False


#######################################################################
#     监听函数
#######################################################################


async def listen(mqtt_client):
    async for message in mqtt_client.messages:
        print(message.payload)

        try:
            payload = json.loads(message.payload.decode())

            # 获取 设备ID
            client_id = str(message.topic).split("/")[1]  # 修改这里
            logger.info(f"接收到来自 {client_id} 的消息")

            # 根据消息类型分发处理
            msg_type = payload.get("type")

            if msg_type == "hello":
                await handle_hello(mqtt_client, client_id, payload)

            elif msg_type == "goodbye":
                await handle_goodbye(mqtt_client, client_id, payload)

        except json.JSONDecodeError:
            logger.info("无效的JSON格式")
        except Exception as e:
            logger.info(f"处理消息时出错: {str(e)}")


#######################################################################
#     消息处理函数
#######################################################################


async def handle_hello(mqtt_client: str, client_id: str, payload: dict):
    """处理设备连接请求"""

    try:
        logger.info("处理hello消息")

        transport = payload.get("transport")
        if transport != "udp":
            logger.error(f"设备传输类型不是udp , 目前服务器只支持 udp")

        await set_client_subscription(client_id)

        # 设置client设备的订阅地址
        if client_id not in client_subscriptions:
            # 假设订阅地址为 "device/{client_id}/#"
            subscribe_topic = f"device_pub/{client_id}"
            client_subscriptions[client_id] = subscribe_topic
            logger.info(f"新增client_id订阅记录: {client_id} -> {subscribe_topic}")

        # 定义session id
        session_id = None

        # 获取音频参数
        audio_params = payload.get("audio_params", {})
        input_sample_rate = audio_params.get("sample_rate", 16000)
        channels = audio_params.get("channels", 1)
        frame_duration = audio_params.get("frame_duration", 60)

        # 查询设备ID
        device_id = await get_device_by_client(client_id)
        if not device_id:
            logger.error(f"未找到client_id")

        # 查询用户是否还处于活跃
        session_id = await get_device_data(device_id, "session_id")
        if session_id:

            logger.info(f"设备已有session, 返回已有session数据 {session_id}")

            # 获取已有 SESSION UDP 数据
            udp_message = await get_udp_pool_info(session_id)

            # 获取UDP通道数据
            udp_address = udp_message["udp_address"]
            udp_port = udp_message["udp_port"]
            udp_key = udp_message["key"]
            udp_nonce = udp_message["nonce"]

            # 打包 Hello 消息帧
            hello_msg = package_message(
                "hello",
                transport="udp",
                session_id=session_id,  # 实际传入具体的session_id值
                audio_params={
                    "sample_rate": 16000,
                    "frame_duration": 60,
                },
                udp={
                    "server": udp_address,
                    "port": udp_port,
                    "encryption": "aes-128-ctr",
                    "key": udp_key,
                    "nonce": udp_nonce,
                },
            )

            # 发送 Hello 消息帧
            await manager_send(mqtt_client, client_id, hello_msg)

        else:

            session_id = str(uuid.uuid4())

            logger.info("设备无session, 创建UDP通讯流程")
            await update_device_field(
                device_id, "session_id", session_id
            )  # 更新设备 session_id

            # 创建UDP通道
            udp_message = await create_udp_audio_channel(
                session_id=session_id,
                input_sample_rate=input_sample_rate,
                channels=channels,
                frame_duration=frame_duration,
            )

            # 获取UDP通道数据
            udp_address = udp_message["udp_address"]
            udp_port = udp_message["udp_port"]
            udp_key = udp_message["key"]
            udp_nonce = udp_message["nonce"]

            # 查询用户, 更新收费服务
            username = await get_device_data(device_id, "username")
            if not username:
                logger.error(f"未找到用户")

            # 创建 Session 对话 , 特殊功能加入到session会话中
            session_data = {
                "session_id": session_id,
                "client_id": client_id,
                "username": username,
                "tts_role": "saike",
                "role": "客户",
            }

            await dao_create_session(session_data)

            # 返回 Hello 消息帧
            hello_msg = package_message(
                "hello",
                transport="udp",
                session_id=session_id,  # 实际传入具体的session_id值
                audio_params={
                    "sample_rate": 16000,
                    "frame_duration": 60,
                },
                udp={
                    "server": udp_address,
                    "port": udp_port,
                    "encryption": "aes-128-ctr",
                    "key": udp_key,
                    "nonce": udp_nonce,
                },
            )

            logger.info("返回MQTT消息")
            await manager_send(mqtt_client, client_id, hello_msg)

    except Exception as e:
        logger.info(f"hello 响应异常: {str(e)}")
        return False


async def handle_goodbye(mqtt_client: str, client_id: str, payload: dict):
    """处理设备断开请求"""
    try:
        logger.info("处理goodbye消息")

        # 删除客户端订阅地址全局表
        await delete_client_subscription(client_id)

        # 获取session_id
        session_id = payload.get("session", {})

        # 删除session_id对话
        await dal_delete_session(session_id)

    except Exception as e:
        logger.error(f"删除会话资源出错 {session_id}")


#######################################################################
#    fastapi 接口
#######################################################################


# fastapi 请求类型
class HelloRequest(BaseModel):
    device_id: str
    session_id: str
    udp_address: str
    udp_port: int
    key: str
    nonce: str
    sample_rate: int = 16000
    frame_duration: int = 60


class GoodByeRequest(BaseModel):
    device_id: str
    session_id: str


class TTSStateRequest(BaseModel):
    device_id: str
    session_id: str
    state: str
    text: str = None


class IoTCommandRequest(BaseModel):
    device_id: str
    session_id: str
    commands: list


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    async with aiomqtt.Client(
        hostname=MQTT_BROKER,
        port=MQTT_PORT,
        keepalive=MQTT_KEEPALIVE,
        tls_context=tls_params,  # 使用 TLS 配置
        identifier=CLIENT_ID,
    ) as c:
        # Make client globally available
        client = c
        # Listen for MQTT messages in (unawaited) asyncio task
        await client.subscribe("device/#")
        loop = asyncio.get_event_loop()
        task = loop.create_task(listen(client))
        yield
        # Cancel the task
        task.cancel()
        # Wait for the task to be cancelled
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def publish(client: Annotated[aiomqtt.Client, Depends(get_mqtt)]):
    await client.publish("humidity/outside", 0.38)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )

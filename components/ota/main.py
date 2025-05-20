import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
import aiohttp
import redis.asyncio as redis

#######################################################################
#    配置日志
#######################################################################

# 配置日志记录

logger = logging.getLogger("ota")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "ota.log")

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


#####################
#  模块配置
#####################

## 定义最新的版本固件

UVICORN_HOST = "192.168.0.111"  # FastAPI服务监听地址
UVICORN_PORT = 8000  # FastAPI服务监听端口

CONFIG_LATEST_FIRMWARE_VERSION = "1.5.5"
DAO_URL = "http://192.168.0.111:8005"

DAO_DEVICE_URL = f"{DAO_URL}/devices"  # 设备相关接口基础路径
ACTIVATION_API_PREFIX = f"{DAO_URL}/activation-codes"  # 激活码相关接口基础

REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_ACTIVATION_QUEUE = "ota_activation_queue"


#######################################################################
#    API 函数
#######################################################################
async def create_device_data(device_id: str, client_id: str, ip_address: str) -> bool:
    """调用DAO接口创建设备数据"""
    global DAO_DEVICE_URL

    try:
        async with aiohttp.ClientSession() as session:
            data = {
                "device_id": device_id,
                "client_id": client_id,
                "ip_address": ip_address,
                "username": "",  # 注册流程中填充用户名
                "session_id": "",
            }

            async with session.post(f"{DAO_DEVICE_URL}", json=data) as response:
                if response.status == 200:
                    logger.info(f"设备 {device_id} 创建成功")
                    return True
                logger.error(f"设备创建失败: {await response.text()}")
                return False

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return False


async def get_device_info(device_id: str) -> dict:
    """获取设备详细信息"""
    global DAO_DEVICE_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DAO_DEVICE_URL}", params={"device_id": device_id}
            ) as response:
                if response.status == 200:
                    device_data = await response.json()
                    return device_data
                logger.error(f"设备信息获取失败: {await response.text()}")
                return {}

    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return {}

    except Exception as e:
        logger.error(f"设备信息查询异常: {str(e)}")
        return {}


async def check_device_exists(device_id: str) -> bool:
    """检查设备是否存在"""
    global DAO_DEVICE_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DAO_DEVICE_URL}", params={"device_id": device_id}
            ) as response:
                if response.status == 200:
                    # DAO的get_devices接口返回设备数据 (非空表示存在)
                    device_data = await response.json()
                    logger.info(f"设备 {device_id} 存在")
                    return bool(device_data)
                logger.info(f"设备不存在, 状态码: {response.status}")
                return False
    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return False


async def create_activation_code(device_id: str) -> str:
    """生成设备激活码"""
    global DAO_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DAO_URL}/activation-codes",
                params={
                    "device_id": device_id,
                },
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                logger.error(f"激活码生成失败: {await response.text()}")
                return None
    except Exception as e:
        logger.error(f"激活码生成异常: {str(e)}")
        return None


async def get_activation_code_by_device(device_id: str) -> str:
    """通过设备ID查询激活码"""
    global DAO_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DAO_URL}/activation-code/device-code", params={"device_id": device_id}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
    except Exception as e:
        logger.error(f"激活码查询失败: {str(e)}")

    return ""


async def get_device_by_activation_code(activation_code: str) -> str:
    """通过激活码查询设备ID"""
    global DAO_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DAO_URL}/activation-code/code-device", params={"code": activation_code}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
    except Exception as e:
        logger.error(f"设备ID查询失败: {str(e)}")

    return ""


async def update_device_client(device_id: str, new_client: str) -> bool:
    """更新设备 client_id"""
    global DAO_URL

    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{DAO_URL}/{device_id}/client", params={"new_client": new_client}
            ) as response:
                if response.status == 200:
                    logger.info(f"设备 {device_id} 存在")
                    return True
                logger.info(f"设备不存在")
                return False
    except aiohttp.ClientError as e:
        logger.error(f"网络请求异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return False


#####################
#  响应数据包
#####################

# error response
OTA_ERROR_REPONSE = {"status": "error", "message": "OTA update failed"}
# activate response
OTA_ACTIVATE_REPONSE = {"message": "请使用以下激活码激活设备", "code": "******"}
# mqtt response
OTA_MQTT_REPONSE = {
    "endpoint": "mqtt.example.com",
    "client_id": "**** *** *** ****",
    "username": "",
    "password": "",
    "publish_topic": "device/001/status",
}
# server time response
OTA_SERVER_TIME_REPONSE = {"timestamp": 1629103600, "timezone_offset": 480}
# firmware response
OTA_FIRMWARE_REPONSE = {"version": "1.5.5", "url": "http://192.168.0.111:8000/firmware"}


#####################
# OTA升级
#####################


# 检查固件版本
def check_firmware_version(version):
    """
    检查设备固件版本是否需要升级

    参数:
        version (str) : 设备当前固件版本号

    返回:
        bool: 是否需要升级
            True - 服务器设备最新固件比设备版本新
            False - 设备版本与服务器版本相同

    """
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


#####################
# MQTT 配置部分 , 实际部署应该查询 manager 组件中的mqtt信息
#####################
# MQTT 服务器配置
MQTT_BROKER = "192.168.0.111"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
MQTT_USERNAME = "your_username"  # 替换为实际的用户名
MQTT_PASSWORD = "your_password"  # 替换为实际的密码
CLIENT_ID = "lienji"
SUBSCRIBE_TOPIC = "device/#"


#####################
# OTA 设备注册
#####################


async def check_activation_status(activation_code: str) -> bool:
    """
    检查激活码状态

    参数:
        code (str): 要验证的激活码字符串

    返回:
        bool:
            - True 设备激活成功
            - False 设备未激活
    """
    try:
        device_id = await get_device_by_activation_code(activation_code)
        if not device_id:
            logger.info("索引不存在, 这个设备的激活码还没创建, 退出")
            return False

        device_info = await get_device_info(device_id)
        if not device_info:
            logger.info("设备不存在, 用户还没有激活此设备, 退出")
            return False

        data = device_info.get("username")
        if not data:
            logger.info("设备存在, 用户名不存在, 用户激活错误, 退出")
            return False

        return True

    except Exception as e:
        logger.error(f"激活状态检查异常: {str(e)}")
        return False


async def process_device_activation(
    device_id: str, client_id: str, ip_address: str
) -> bool:
    """
    设备激活激活码处理

    Args:
        device_id (str): 设备唯一标识符 (MAC地址)

    返回:
        bool
            - True   已注册
            - False  未注册, 已生成激活码


    处理流程:
        1. 检查设备信息, 判断设备是否存在
        2. 已激活设备 (含用户名) 直接返回成功
        3. 已存在激活码的设备返回待激活状态
        4. 全新设备 生成激活码并返回
    """

    try:

        # 先检查设备是否存在
        device_exists = await check_device_exists(device_id)

        if not device_exists:
            # 创建设备基础数据
            created = await create_device_data(device_id, client_id, ip_address)
            if not created:
                logger.error("设备数据创建失败")
                return False

        # 检查设备是否存在
        device_info = await get_device_info(device_id)

        # 已有用户数据
        if device_info and device_info.get("username"):
            return True

        # 已有激活码但未注册用户的情况
        if device_info:
            activation_code = await get_activation_code_by_device(device_id)
            if activation_code:
                return False

        # 完全未注册的情况下生成新激活码
        activation_code = await create_activation_code(device_id)
        if activation_code:
            logger.info("注册码生成成功, 退出注册流程")
        else:
            logger.info("注册码生成失败, 请检查DAO模块, 退出注册流程")
        return False

    except Exception as e:
        logger.error(f"设备激活处理异常: {str(e)}")
        return False


#####################
# FastTAPI 部分
#####################


# Redis 监听函数
async def redis_listener(app: FastAPI):
    while True:
        try:
            # 阻塞式获取队列消息
            _, message = await app.state.redis.brpop(REDIS_ACTIVATION_QUEUE)
            device_id = message.decode()
            pass
        except Exception as e:
            logger.error(f"监听异常: {str(e)}")
            await asyncio.sleep(1)


# lifespan 上下文管理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化 Redis 连接
    app.state.redis = redis.Redis(
        connection_pool=redis.ConnectionPool(
            host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False
        )
    )

    try:
        await app.state.redis.ping()
        logger.info("Redis 连接成功")

        # 创建后台任务
        app.state.redis_listener = asyncio.create_task(redis_listener(app))

    except Exception as e:
        logger.error("无法连接到 Redis 服务器: %s", e)
        raise

    yield

    await app.state.redis.aclose()
    app.state.redis_listener.cancel()
    try:
        await app.state.redis_listener
    except asyncio.CancelledError:
        logger.info("Redis listener 已取消")
    except Exception as e:
        logger.error("Redis listener 异常终止: %s", e)


app = FastAPI(lifespan=lifespan)


# 处理根路径的GET和POST请求


@app.api_route("/", methods=["GET", "POST"])
async def handle_root(request: Request):
    """
    处理设备 OTA 请求
    """
    if request.method == "GET":
        # 暂不处理， 应该不会使用GET请求
        logging.info(f"Received GET request at root, but not should be, we pass here")
        pass

    elif request.method == "POST":
        content_type = request.headers.get("Content-Type")
        print("Request Headers:", dict(request.headers))

        reponse = {}  # 存储响应数据
        activation_code = None
        if content_type == "application/json":
            try:

                #    解析数据    #
                data = await request.json()

                client_id = request.headers.get("client-id")
                device_id = request.headers.get("device-id").replace(":", "")
                activation_code = request.headers.get("activation_code")
                firmware_version = data["application"]["version"]

                #    固件信息      #
                # if check_firmware_version(firmware_version):
                #     OTA_FIRMWARE_REPONSE["version"] = CONFIG_LATEST_FIRMWARE_VERSION
                #     reponse["firmware"] = OTA_FIRMWARE_REPONSE  # 存放固件信息
                #     return reponse  # 返回固件升级信息
                # else:
                #     logger.info(f"设备 {device_id} 已是最新版本, 无需升级")

                reponse["firmware"] = OTA_FIRMWARE_REPONSE  # 存放固件信息

                #    已有激活码, 会执行这个函数
                if activation_code:
                    if check_activation_status(activation_code):
                        logger.info("用户已激活设备, 可以返回MQTT信息")
                else:
                    logger.info("进入设备激活流程")
                    #    设备注册流程,创建激活码  #
                    if await process_device_activation(
                        device_id,
                        client_id,
                        f"{request.client.host}:{request.client.port}",
                    ):
                        logger.info(f"设备无需激活, 可以返回MQTT信息")
                    else:
                        logger.info(f"设备等待激活, 请前往用户前端页面进行注册")
                        activation_code = await get_activation_code_by_device(device_id)
                        if activation_code:
                            OTA_ACTIVATE_REPONSE["code"] = activation_code
                            OTA_ACTIVATE_REPONSE["message"] = (
                                f"请输出激活码{activation_code}激活设备"
                            )
                            reponse["activation"] = (
                                OTA_ACTIVATE_REPONSE  # 存放激活码信息
                            )
                            return reponse  # 返回激活码信息
                        else:
                            logger.info(
                                "process_device_activation 函数中actiavte code 创建失败, 检查DAO模块"
                            )
                            return {}

                #    MQTT信息      #
                OTA_MQTT_REPONSE["endpoint"] = MQTT_BROKER
                OTA_MQTT_REPONSE["client_id"] = client_id
                OTA_MQTT_REPONSE["username"] = ""
                OTA_MQTT_REPONSE["password"] = ""
                OTA_MQTT_REPONSE["publish_topic"] = f"device/{client_id}"
                reponse["mqtt"] = OTA_MQTT_REPONSE
                # reponse["servertime"] = OTA_SERVER_TIME_REPONSE  # 存放服务器时间信息

                return reponse  # 返回MQTT信息

            except json.JSONDecodeError as e:
                logging.error(f"Error parsing JSON data at root: {str(e)}")

        return {}


# 响应二进制固件数据, 用于固件升级
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

    uvicorn.run(
        "main:app",
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )

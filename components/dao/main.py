import asyncio
import uuid
import logging
import os
import json
import redis.asyncio as redis
from fastapi import FastAPI, File, HTTPException, Depends, Query, Response, UploadFile
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from starlette.requests import Request
import secrets

#####################
#  配置日志
#####################

# 配置日志记录

logger = logging.getLogger("dao")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

log_file_path = os.path.join(log_dir, "dao.log")

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
UVICORN_HOST = "192.168.0.111"  # FastAPI服务监听地址
UVICORN_PORT = 8005  # FastAPI服务监听端口

## 定义一个 设备集合 用于保存 device id , 除非服务器重启 ， 否则一直在内存中保存会话数据
## 包含设备的session_id 和 用户数据
DEVICE_ID_SET = "device_queue"
ACTIVATE_CODE_SET = "activate_queue"

## 定义TTS session 队列
TTS_INPUT_QUEUE_KEY = "tts_input_queue"
TTS_OUTPUT_QUEUE_KEY = "tts_output_queue"
TTS_SESSION_QUEUE = "tts_queue"
ASR_SESSION_QUEUE = "asr_queue"
ASR_INPUT_QUEUE_KEY = "asr_input_queue"
ASR_OUTPUT_QUEUE_KEY = "asr_output_queue"


## 定义数据索引
CLIENT_TO_DEVICE_INDEX = "client_to_device_index"
IP_TO_DEVICE_INDEX = "ip_to_device_index"
CLIENT_TO_SESSION_INDEX = (
    "client_to_session_index"  # 新增: client_id 到 session_id 的索引
)

## 会话默认过期时间 (单位 秒 , 默认十分钟)
SESSION_EXPIRE_TIME = 300


############################################################################################
#
#                               @@  生命周期和过期事件处理
#     TODO : 向redis配置文件 redis.conf 中添加 notify-keyspace-events Ex 启用过期事件 键空间通知
#     Ex   E: 表示启用过期事件,  X: 表示键空间通知
#
############################################################################################


async def listen_for_expired_events():
    async for message in app.state.pubsub.listen():
        logger.info(f"监听到事件 {message}")
        if message["type"] == "message":
            expired_key: str = message["data"].decode()  # 过期
            logger.info(f"监听到过期事件 {expired_key}")
            if expired_key.startswith("session:"):
                session_id = expired_key.split(":")[1]
                logger.info(f"会话过期, 清理关联设备的 session_id: {session_id}")

                # 1. 获取 session 的 client_id
                session_dao = await get_session_dao()
                session_data = await session_dao.get_session(session_id)
                if not session_data:
                    logger.warning(f"过期 session 无数据: {session_id}")
                    continue
                client_id = session_data.get("client_id")

                # 2. 通过client_id查找对应的device_id
                device_dao = await get_device_dao()
                device_id = await device_dao.get_device_by_client_id(client_id)
                if not device_id:
                    logger.warning(f"未找到client_id关联的device: {client_id}")
                    continue
                device_id = device_id.decode()  # 转换为字符串

                # 3. 清理设备的session_id (保持其他字段不变, 仅清空session_id)
                current_device = await device_dao.get_device(device_id)
                if not current_device:
                    logger.warning(f"设备不存在: {device_id}")
                    continue
                await device_dao.update_device(
                    device_id=device_id,
                    client_id=current_device["client_id"],
                    ip_address=current_device["ip_address"],
                    username=current_device["username"],
                    session_id="",  # 关闭修改: 将session_id置空
                )
                logger.info(f"成功清理设备 {device_id} 的session_id")

                # 发送 goodbye 消息
                logger.info(f"会话过期, 向agent发送goodbye {session_id}")


# 应用生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 连接到 Redis 服务器
    app.state.redis = redis.Redis(
        connection_pool=redis.ConnectionPool(
            host="localhost", port=6379, db=0, decode_responses=False
        )
    )

    # Redis发布订阅实例
    app.state.pubsub = app.state.redis.pubsub()

    try:
        # 测试连接是否成功
        await app.state.redis.ping()
        logging.info("Successfully connected to Redis")

        # 订阅session获取事件 (键空间通知格式: __keyevent@<db>__:expired)
        # 监听所有过期事件
        await app.state.pubsub.subscribe("__keyevent@0__:expired")

        # 启动后台监听订阅消息
        app.state.expire_listener = asyncio.create_task(listen_for_expired_events())

    except Exception as e:
        logging.error(f"Could not connect to Redis: {e}")
        raise

    yield    # 应用在此运行

    try:
        device_dao = await get_device_dao()
        async for key in app.state.redis.scan_iter(match="device:*"):
            _, device_id = key.decode().split(":", 1)
            current_device = await device_dao.get_device(device_id)
            if current_device:
                # 保持其他字段不变, 仅清空session_id
                await device_dao.update_device(
                    device_id=device_id,
                    client_id=current_device["client_id"],
                    ip_address=current_device["ip_address"],
                    username=current_device["username"],
                    session_id="",
                )
                logger.info(f"程序退出时清理设备 {device_id} 的session_id")

    except Exception as e:
        logger.error(f"程序退出时清理设备session_id失败: {str(e)}")

    await app.state.pubsub.unsubscribe("__keyevent@0__:expired")
    await app.state.pubsub.close()
    app.state.expire_listener.cancel()

    await app.state.redis.close()


app = FastAPI(lifespan=lifespan)


# 新增依赖项获取器
async def get_dao():
    return TTSDao(redis_conn=app.state.redis)


############################################################################################
#
#                               @@  Session 会话数据访问对象
#
############################################################################################


class SessionBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    client_id: str = Field(..., description="客户端ID")
    username: str = Field(..., description="用户名")
    tts_role: str = Field(..., description="TTS角色")
    role: str = Field(..., description="用户身份标识")


class SessionDao:
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn

    async def create_session(
        self, session_id: str, client_id: str, username: str, tts_role: str, role: str
    ):
        """创建/更新会话配置"""

        async with self.redis.pipeline(transaction=True) as pipe:
            logger.info(f"创建会话 会话id:{session_id}")
            await pipe.hset(
                f"session:{session_id}",
                mapping={
                    "client_id": client_id,
                    "username": username,
                    "tts_role": tts_role,
                    "role": role,
                },
            )
            await pipe.hset(CLIENT_TO_SESSION_INDEX, client_id, session_id)
            await pipe.expire(f"session:{session_id}", SESSION_EXPIRE_TIME)
            await pipe.execute()

    async def update_session(
        self, session_id: str, client_id: str, username: str, tts_role: str, role: str
    ):
        """更新会话"""
        async with self.redis.pipeline(transaction=True) as pipe:
            logger.info(f"创建会话 会话id:{session_id}")
            await pipe.hset(
                f"session:{session_id}",
                mapping={
                    "client_id": client_id,
                    "username": username,
                    "tts_role": tts_role,
                    "role": role,
                },
            )
            await pipe.hset(CLIENT_TO_SESSION_INDEX, client_id, session_id)
            await pipe.expire(f"session:{session_id}", SESSION_EXPIRE_TIME)
            await pipe.execute()

    async def get_session(self, session_id: str) -> dict:
        """获取完整会话配置"""
        logger.info(f"获取会话 会话id:{session_id}")
        data = await self.redis.hgetall(f"session:{session_id}")
        if not data:
            return None
        return {
            "session_id": session_id,
            "client_id": data.get(b"client_id", b"").decode(),
            "username": data.get(b"username", b"").decode(),
            "tts_role": data.get(b"tts_role", b"").decode(),
            "role": data.get(b"role", b"").decode(),
        }

    async def delete_session(self, session_id: str):
        """删除会话配置"""

        logger.info(f"删除会话 会话id:{session_id}")

        async with self.redis.pipeline(transaction=True) as pipe:
            # 获取当前会话的client_id（用于清理索引）
            session_data = await pipe.hgetall(f"session:{session_id}")
            client_id = session_data.get(b"client_id", b"").decode()
            await pipe.hdel(CLIENT_TO_SESSION_INDEX, client_id)
            await pipe.delete(f"session:{session_id}")
            await pipe.execute()

    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        return await self.redis.exists(f"session:{session_id}") == 1

    async def refresh_session(self, session_id: str):
        """刷新会话过期时间"""
        success = await self.redis.expire(f"session:{session_id}", SESSION_EXPIRE_TIME)
        return success == 1


# Session 依赖项获取器
async def get_session_dao():
    return SessionDao(redis_conn=app.state.redis)


############################
# Session 会话数据接口
############################


@app.post("/sessions")
async def create_session(
    session_base: SessionBase,
    dao: SessionDao = Depends(get_session_dao),
):
    try:
        await dao.create_session(
            session_id=session_base.session_id,
            client_id=session_base.client_id,
            tts_role=session_base.tts_role,
            username=session_base.username,
            role=session_base.role,
        )

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/sessions")
async def update_session(
    session_base: SessionBase,
    dao: SessionDao = Depends(get_session_dao),
):
    pass


@app.get("/sessions")
async def get_session(
    session_id: str | None = Query(
        None, description="可选的会话ID, 为空时查询所有会话"
    ),
    dao: SessionDao = Depends(get_session_dao),
):
    try:

        if session_id:
            session_data = await dao.get_session(session_id)
            if not session_data:
                raise HTTPException(status_code=404, detail="Session 没找到")
            return session_data

        else:
            all_sessions = []
            async for key in dao.redis.scan_iter(
                match="session:*"
            ):  # 扫描所有session键
                _, sid = key.decode().split(":", 1)
                session_item = await dao.get_session(sid)
                if session_item:
                    all_sessions.append(session_item)
            return all_sessions

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/sessions")
async def delete_session(
    session_id: str = Query(..., description="删除会话"),
    dao: SessionDao = Depends(get_session_dao),
):
    try:
        if not await dao.session_exists(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        await dao.delete_session(session_id)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions/refresh")
async def refresh_session(
    session_id: str = Query(..., description="需要刷新的会话ID"),
    dao: SessionDao = Depends(get_session_dao),
):
    try:
        if not await dao.session_exists(session_id):
            raise HTTPException(status_code=404, detail="Session不存在")

        if not await dao.refresh_session(session_id):
            raise HTTPException(status_code=500, detail="刷新过期时间失败")

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


############################################################################################
#
#                               @@  TTS队列数据访问对象
#
############################################################################################


class TTSBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    status: str = Field(..., description="TTS条目状态")
    audio: bytes = Field(..., description="音频二进制数据")
    text: str = Field(..., description="文本数据")


class TTSDao:
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn

    async def create_tts_item(
        self, session_id: str, status: str, audio: bytes, text: str
    ):
        """创建新的TTS条目"""

        # 将 session_id 存入队列 (使用Redis列表)
        await self.redis.lpush(TTS_INPUT_QUEUE_KEY, session_id)

        # 存储tts数据到hash
        await self.redis.hset(
            f"tts:{session_id}",
            mapping={
                "status": status,  # 还未生成音频
                "audio": audio,  # 音频数据
                "text": text,  # 文本内容
            },
        )

    async def update_tts_item(
        self, session_id: str, status: str, audio: bytes, text: str
    ):
        await self.redis.hset(
            f"tts:{session_id}",
            mapping={
                "status": status,
                "audio": audio,
                "text": text,
            },
        )

    async def get_tts_item(self, session_id: str) -> dict:
        """获取完整的TTS条目数据"""
        data = await self.redis.hgetall(f"tts:{session_id}")
        return {
            "session_id": session_id,
            "status": data.get(b"status", b"False").decode(),
            "text": data.get(b"text", b"").decode(),
            "audio": data.get(b"audio", b""),
        }

    async def tts_exists(self, session_id: str) -> bool:
        """检查TTS条目是否存在"""
        return await self.redis.exists(f"tts:{session_id}") == 1

    async def is_input_queue_empty(self) -> bool:
        """检查TTS队列是否为空"""
        length = await self.redis.llen(TTS_INPUT_QUEUE_KEY)
        return length == 0

    async def is_output_queue_empty(self) -> bool:
        length = await self.redis.llen(TTS_OUTPUT_QUEUE_KEY)
        return length == 0


# 新增 TTS 依赖注入
async def get_tts_dao():
    return TTSDao(redis_conn=app.state.redis)


################
# TTS 队列接口
################


# 新建TTS条目
@app.post("/tts")
async def create_tts_item(
    session_id: str = Query(..., description="会话ID"),
    status: str = Query(..., description="条目状态"),
    audio: UploadFile = File(..., description="音频文件"),
    text: str = Query(..., description="文本数据"),
    dao: TTSDao = Depends(get_tts_dao),
):
    try:
        # 检查文件大小
        content_length = int(audio.headers.get("content-length", 0))
        if content_length > 10 * 1024 * 1024:  # 10MB
            raise HTTPException(status_code=413, detail="文件过大")

        # 读取文件内容
        audio_bytes = await audio.read()

        await dao.create_tts_item(session_id, status, audio_bytes, text)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# 更新TTS条目
@app.put("/tts")
async def update_tts_item(
    session_id: str = Query(..., description="会话ID"),
    status: str = Query(..., description="条目状态"),
    audio: UploadFile = File(..., description="音频文件"),
    text: str = Query(..., description="文本数据"),
    dao: TTSDao = Depends(get_tts_dao),
):
    try:
        # 检查文件大小
        content_length = int(audio.headers.get("content-length", 0))
        if content_length > 10 * 1024 * 1024:  # 10MB
            raise HTTPException(status_code=413, detail="文件过大")

        # 读取文件内容
        audio_bytes = await audio.read()

        await dao.update_tts_item(session_id, status, audio_bytes, text)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# 查询TTS条目
@app.get("/tts")
async def get_tts_item(
    session_id: str | None = Query(
        None, description="可选的会话ID, 为空时查询所有TTS条目"
    ),
    dao: TTSDao = Depends(get_tts_dao),
):
    try:
        if session_id:
            # 查询单个条目
            tts_data = await dao.get_tts_item(session_id)
            if not tts_data:
                raise HTTPException(status_code=404, detail="TTS条目不存在")
            return tts_data
        else:
            all_tts = []
            async for key in dao.redis.scan_iter(match="tts:*"):
                _, sid = key.decode().split(":", 1)
                tts_item = await dao.get_tts_item(sid)
                if tts_item:
                    all_tts.append(tts_item)
            return all_tts
    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# 删除 TTS 条目
@app.delete("/tts")
async def delete_tts_item(
    session_id: str = Query(..., description="可选的会话ID, 为空时查询所有TTS条目"),
    dao: TTSDao = Depends(get_tts_dao),
):
    try:
        if not await dao.tts_exists(session_id):
            raise HTTPException(status_code=404, detail="TTS条目不存在")
        await dao.redis.delete(f"tts:{session_id}")

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tts/queue/empty")
async def check_tts_queue_empty(
    queue_type: str | None = Query(
        None, description="队列类型 (input: 输入队列, output: 输出队列)"
    ),
    dao: TTSDao = Depends(get_tts_dao),
):
    """检查TTS队列是否为空"""
    try:
        if queue_type == "input":
            empty = await dao.is_input_queue_empty()
        elif queue_type == "output":
            empty = await dao.is_output_queue_empty()
        else:
            raise HTTPException(
                status_code=400, detail="无效的队列类型, 仅支持input/output"
            )
        return empty

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/queue")
async def tts_queue_operation(
    queue_type: str | None = Query(None, description="队列类型"),
    session_id: str | None = Query(
        None, description="会话ID (存在时为push, 不存在时为pop)"
    ),
    dao: TTSDao = Depends(get_tts_dao),
):
    """TTS队列统一操作接口"""
    try:
        if queue_type not in ["input", "output"]:
            raise HTTPException(
                status_code=400, detail="无效的队列类型, 仅支持 input/output"
            )

        # 选择目标队列键值
        target_queue = (
            TTS_INPUT_QUEUE_KEY if queue_type == "input" else TTS_OUTPUT_QUEUE_KEY
        )

        if session_id:
            await dao.redis.lpush(target_queue, session_id)

        else:
            session_id = await dao.redis.rpop(target_queue)
            if not session_id:
                raise HTTPException(status_code=404, detail=f"{queue_type}队列无数据")
            return session_id.decode()

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


############################################################################################
#
#                               @@  ASR队列数据访问对象
#
############################################################################################


class ASRBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    status: str = Field(..., description="ASR条目状态")
    audio: bytes = Field(..., description="音频二进制数据")
    text: str = Field(..., description="文本数据")


class ASRDao:
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn

    async def create_asr_item(
        self, session_id: str, status: str, audio: bytes, text: str
    ):
        """创建新的ASR条目"""
        await self.redis.lpush(ASR_INPUT_QUEUE_KEY, session_id)
        await self.redis.hset(
            f"asr:{session_id}",
            mapping={
                "status": status,
                "audio": audio,
                "text": text,
            },
        )

    async def update_asr_item(
        self, session_id: str, status: str, audio: bytes, text: str
    ):
        await self.redis.hset(
            f"asr:{session_id}",
            mapping={
                "status": status,
                "audio": audio,
                "text": text,
            },
        )

    async def get_asr_item(self, session_id: str) -> dict:
        """获取完整的ASR条目数据"""
        data = await self.redis.hgetall(f"asr:{session_id}")
        return {
            "session_id": session_id,
            "status": data.get(b"status", b"False").decode(),
            "text": data.get(b"text", b"").decode(),
            "audio": data.get(b"audio", b""),
        }

    async def is_input_queue_empty(self) -> bool:
        """检查ASR输入队列是否为空"""
        length = await self.redis.llen(ASR_INPUT_QUEUE_KEY)
        return length == 0

    async def is_output_queue_empty(self) -> bool:
        """检查ASR输出队列是否为空"""
        length = await self.redis.llen(ASR_OUTPUT_QUEUE_KEY)
        return length == 0

    async def asr_exists(self, session_id: str) -> bool:
        """检查ASR条目是否存在"""
        return await self.redis.exists(f"asr:{session_id}") == 1


# 新增 ASR 依赖注入
async def get_asr_dao():
    return ASRDao(redis_conn=app.state.redis)


################
# ASR 队列接口
################
@app.post("/asr")
async def create_asr_item(
    session_id: str = Query(..., description="会话ID"),
    status: str = Query(..., description="条目状态"),
    audio: UploadFile = File(..., description="音频文件"),
    text: str = Query(..., description="文本数据"),
    dao: ASRDao = Depends(get_asr_dao),
):
    try:
        # 检查文件大小
        content_length = int(audio.headers.get("content-length", 0))
        if content_length > 10 * 1024 * 1024:  # 10MB
            raise HTTPException(status_code=413, detail="文件过大")

        # 读取文件内容
        audio_bytes = await audio.read()

        await dao.create_asr_item(session_id, status, audio_bytes, text)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/asr")
async def update_asr_item(
    session_id: str = Query(..., description="会话ID"),
    status: str = Query(..., description="条目状态"),
    audio: UploadFile = File(..., description="音频文件"),
    text: str = Query(..., description="文本数据"),
    dao: ASRDao = Depends(get_asr_dao),
):
    try:
        # 检查文件大小
        content_length = int(audio.headers.get("content-length", 0))
        if content_length > 10 * 1024 * 1024:  # 10MB
            raise HTTPException(status_code=413, detail="文件过大")

        # 读取文件内容
        audio_bytes = await audio.read()

        await dao.update_asr_item(session_id, status, audio_bytes, text)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/asr")
async def get_asr_item(
    session_id: str | None = Query(
        None, description="可选的会话ID, 为空时查询所有ASR条目"
    ),
    dao: ASRDao = Depends(get_asr_dao),
):
    try:
        if session_id:
            asr_data = await dao.get_asr_item(session_id)
            if not asr_data:
                raise HTTPException(status_code=404, detail="ASR条目不存在")
            return asr_data

        else:
            all_asr = []
            async for key in dao.redis.scan_iter(match="asr:*"):
                _, sid = key.decode().split(":", 1)
                asr_item = await dao.get_asr_item(sid)
                if asr_item:
                    all_asr.append(asr_item)
            return all_asr

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/asr")
async def delete_asr_item(
    session_id: str = Query(..., description="会话ID"),
    dao: ASRDao = Depends(get_asr_dao),
):
    try:
        if not await dao.asr_exists(session_id):
            raise HTTPException(status_code=404, detail="ASR条目不存在")
        await dao.redis.delete(f"asr:{session_id}")

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("asr/queue/empty", description="判断队列是否为空")
async def check_asr_queue_empty(
    queue_type: str | None = Query(None, description="队列类型 (input/output)"),
    dao: ASRDao = Depends(get_asr_dao),
):
    """见擦汗ASR队列是否为空"""
    try:
        if queue_type == "input":
            empty = await dao.is_input_queue_empty()
        elif queue_type == "output":
            empty = await dao.is_output_queue_empty()
        else:
            raise HTTPException(
                status_code=400, detail="无效的队列类型, 仅支持input/output"
            )
        return empty

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/asr/queue")
async def asr_queue_operation(
    queue_type: str = Query("input", description="队列数据 (input/outpu)"),
    session_id: str | None = Query(
        None, description="会话ID (存在时为push, 不存在时为pop)"
    ),
    dao: ASRDao = Depends(get_asr_dao),
):
    """ASR队列统一操作接口 (push/pop)"""
    try:
        # 校验队列类型
        if queue_type not in ["input", "output"]:
            raise HTTPException(
                status_code=404, detail="无效的队列类型, 仅支持input/output"
            )

        # 选择目标队列键 (注意修正原 ASR 队列方法的拼写错误)
        target_queue = (
            ASR_INPUT_QUEUE_KEY if queue_type == "input" else ASR_OUTPUT_QUEUE_KEY
        )

        if session_id:  # push
            await dao.redis.lpush(target_queue, session_id)

        else:  # pop
            session_id = await dao.redis.rpop(target_queue)
            if not session_id:
                raise HTTPException(status_code=404, detail=f"{queue_type}队列无数据")
            return session_id.decode()

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


############################################################################################
#
#                               @@  Device 设备数据访问对象
#
############################################################################################


class DeviceBase(BaseModel):
    device_id: str = Field(..., description="设备唯一ID(MAC地址)")
    client_id: str = Field(..., description="MQTT客户端ID,用于创建会话")
    ip_address: str = Field(
        ..., description="设备IP地址"
    )  # 用于多客户端同时使用一个UDP通道， UDP通道和所有客户端的音频参数配置一样
    username: str = Field(..., description="设备用户名")
    session_id: str = Field(..., description="唯一会话ID")


class DeviceDao:
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn

    async def create_device(
        self,
        device_id: str,
        client_id: str,
        ip_address: str,
        username: str,
        session_id: str,
    ):
        """创建设备数据"""
        async with self.redis.pipeline(transaction=True) as pipe:

            await pipe.hset(
                f"device:{device_id}",
                mapping={
                    "client_id": client_id,
                    "ip_address": ip_address,
                    "username": username,
                    "session_id": session_id,
                },
            )

            # 创建 client_id 到 device_id 的索引
            await pipe.hset(CLIENT_TO_DEVICE_INDEX, client_id, device_id)
            # 创建 ip_address 到 device_id 的索引
            await pipe.hset(IP_TO_DEVICE_INDEX, ip_address, device_id)

            await pipe.sadd(DEVICE_ID_SET, device_id)
            await pipe.execute()

    async def update_device(
        self,
        device_id: str,
        client_id: str,
        ip_address: str,
        username: str,
        session_id: str,
    ):
        """设备数据更新"""
        async with self.redis.pipeline(transaction=True) as pipe:
            await pipe.hset(
                f"device:{device_id}",
                mapping={
                    "client_id": client_id,
                    "ip_address": ip_address,
                    "username": username,
                    "session_id": session_id,
                },
            )

            await pipe.hset(CLIENT_TO_DEVICE_INDEX, client_id, device_id)
            await pipe.hset(IP_TO_DEVICE_INDEX, ip_address, device_id)
            await pipe.execute()

    async def get_device(self, device_id: str) -> dict:
        """获取完整设备信息"""
        data = await self.redis.hgetall(f"device:{device_id}")
        if not data:
            return None

        return {
            "device_id": device_id,
            "client_id": data.get(b"client_id", b"").decode(),
            "ip_address": data.get(b"ip_address", b"").decode(),
            "username": data.get(b"username", b"").decode(),
            "session_id": data.get(b"session_id", b"").decode(),
        }

    async def device_exists(self, device_id: str) -> bool:
        """检查设备是否存在"""
        return await self.redis.sismember(DEVICE_ID_SET, device_id)

    async def delete_device(self, device_id: str):
        """删除设备记录"""
        await self.redis.delete(f"device:{device_id}")
        await self.redis.srem(DEVICE_ID_SET, device_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            device_data = await pipe.hgetall(f"device:{device_id}")
            await pipe.delete(f"device:{device_id}")
            await pipe.srem(DEVICE_ID_SET, device_id)
            await pipe.execute()  # 先执行，获取设备参数, 再继续操作

            # 提取client_id和ip_address（注意处理空值）
            client_id = device_data.get(b"client_id", b"").decode()
            ip_address = device_data.get(b"ip_address", b"").decode()

            async with self.redis.pipeline(transaction=True) as cleanup_pipe:
                await cleanup_pipe.hdel(CLIENT_TO_DEVICE_INDEX, client_id)
                await cleanup_pipe.hdel(IP_TO_DEVICE_INDEX, ip_address)
                await cleanup_pipe.execute()

    async def get_device_by_client_id(self, client_id: str) -> str:
        """通过client_id获取设备ID"""
        return await self.redis.hget(CLIENT_TO_DEVICE_INDEX, client_id)

    async def get_device_by_ip_address(self, ip_address: str) -> str:
        """通过IP地址获取设备ID"""
        return await self.redis.hget(IP_TO_DEVICE_INDEX, ip_address)


# Device 依赖项获取
async def get_device_dao() -> DeviceDao:
    return DeviceDao(redis_conn=app.state.redis)


################
# Device 设备接口
################


@app.post("/devices")
async def create_device(
    device_base: DeviceBase, dao: DeviceDao = Depends(get_device_dao)
):
    try:
        await dao.create_device(
            device_id=device_base.device_id,
            client_id=device_base.client_id,
            ip_address=device_base.ip_address,
            username=device_base.username,
            session_id=device_base.session_id,
        )

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/devices")
async def update_devices(
    device_base: DeviceBase, dao: DeviceDao = Depends(get_device_dao)
):
    # 获取当前设备信息
    current_device = await dao.get_device(device_base.device_id)
    if not current_device:
        raise HTTPException(status_code=404, detail="Device不存在")

    try:
        await dao.update_device(
            device_id=device_base.device_id,
            client_id=device_base.client_id,
            ip_address=device_base.ip_address,
            username=device_base.username,
            session_id=device_base.session_id,
        )

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="Redis服务失败")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/devices")
async def get_devices(
    device_id: str | None = Query(None, description="通过客户端ID查询设备"),
    dao: DeviceDao = Depends(get_device_dao),
):
    try:
        if device_id:
            device_data = await dao.get_device(device_id)
            if not device_data:
                raise HTTPException(status_code=404, detail="Device不存在")
            return device_data

        else:
            all_devices = []
            async for key in dao.redis.scan_iter(match="device:*"):
                _, dev_id = key.decode().split(":", 1)
                device_item = await dao.get_device(dev_id)
                if device_item:
                    all_devices.append(device_item)
            return all_devices

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="Redis服务失败")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/devices")
async def delete_device(
    device_id: str = Query(..., description="需要删除的设备ID"),
    dao: DeviceDao = Depends(get_device_dao),
):
    try:
        if not await dao.device_exists(device_id):
            raise HTTPException(status_code=404, detail="Device not found")

        await dao.delete_device(device_id)

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="Redis服务失败")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/devices/client", description="通过client_id查询设备ID")
async def get_device_by_client_id(
    client_id: str = Query(..., description="用于查询的MQTT客户端ID (必填)"),
    dao: DeviceDao = Depends(get_device_dao),
):
    try:
        device_id = await dao.get_device_by_client_id(client_id)
        if not device_id:
            raise HTTPException(status_code=404, detail="未找到对应设备ID")
        return device_id.decode()  # Redis 返回的是 bytes, 需解码为 str

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


############################################################################################
#
#                                @@  激活码 据访问对象
#
############################################################################################


class ActivateCodeBase(BaseModel):
    activate_code: str = Field(..., description="设备激活码")
    device_id: str = Field(..., description="激活码对应的设备ID")


class ActivateCodeDao:
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn

    async def create_activation_code(self, device_id: str) -> str:
        """生成6位激活码"""
        code = f"{secrets.randbelow(900000)+100000:06}"  # 生成6位数字

        # 建立关联: 激活码->设备  设备->激活码
        await self.redis.setex(f"activation:{code}", 43200, device_id)  # 12小时有效
        await self.redis.setex(f"device_activation:{device_id}", 43200, code)

        # 将激活码加入全局集合
        await self.redis.sadd(ACTIVATE_CODE_SET, code)

        return code

    async def delete_activation_code(self, code: str):
        """删除激活码及其关联"""
        device_id = await self.redis.get(f"activation:{code}")
        if device_id:
            # 使用事务管道
            async with self.redis.pipeline(transaction=True) as pipe:

                # 删除双向关联
                await pipe.delete(f"activation:{code}")
                await pipe.delete(f"device_activation:{device_id.decode()}")

                # 从全局集合移除
                await self.redis.srem(ACTIVATE_CODE_SET, code)

                # 执行所有命令
                await pipe.execute()

    async def get_device_by_activation(self, code: str) -> str:
        """通过激活码查找设备ID"""
        return await self.redis.get(f"activation:{code}")

    async def get_activation_by_device(self, device_id: str) -> str:
        """通过设备ID获取激活码"""
        return await self.redis.get(f"device_activation:{device_id}")


async def get_activate_code_dao():
    return ActivateCodeDao(redis_conn=app.state.redis)


@app.post("/activation-codes", description="生成激活码")
async def create_activation_code(
    device_id: str = Query(..., description="激活码对应的设备"),
    dao: ActivateCodeDao = Depends(get_activate_code_dao),
):
    try:
        code = await dao.create_activation_code(
            device_id,
        )
        return code

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/activation-codes", description="删除激活码")
async def delete_activation_code(
    code: str = Query(..., description="需要删除的激活码 (必填)"),
    dao: ActivateCodeDao = Depends(get_activate_code_dao),
):
    try:
        await dao.delete_activation_code(code)
    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/activation-code/code-device", description="通过激活码查询设备ID")
async def get_device_by_activation_code(
    code: str = Query(..., description="用于查询的激活码 (必填)"),
    dao: ActivateCodeDao = Depends(get_activate_code_dao),
):
    try:
        device_id = await dao.get_device_by_activation(code)
        if not device_id:
            raise HTTPException(status_code=404, detail="激活码不存在")
        return device_id.decode()

    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/activation-code/device-code", description="通过设备查询激活码")
async def get_activation_code_by_device(
    device_id: str = Query(..., description="用于查询的设备ID (必填)"),
    dao: ActivateCodeDao = Depends(get_activate_code_dao),
):
    try:
        code = await dao.get_activation_by_device(device_id)
        if not code:
            raise HTTPException(status_code=404, detail="设备无激活码")
        return code.decode()
    except redis.RedisError as e:
        logger.error(f"Redis操作失败: {str(e)}")
        raise HTTPException(status_code=505, detail="存储服务暂时不可用")

    except ValueError as e:
        logger.warning(f"无效输入: {str(e)}")
        raise HTTPException(status_code=400, detail="无效的输入参数")

    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


################
# 主函数
################
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )

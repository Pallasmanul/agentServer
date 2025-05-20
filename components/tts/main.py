import asyncio
from contextlib import asynccontextmanager
import os
import tempfile
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import pyttsx3
from pydub import AudioSegment
import io

import redis.asyncio as redis

import logging

#######################################################################
#    配置日志
#######################################################################

# 配置日志记录

logger = logging.getLogger("tts_server")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "tts_server.log")

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
UVICORN_HOST = "192.168.0.111"  # FastAPI服务监听地址
UVICORN_PORT = 8003  # FastAPI服务监听端口

TTS_SERVER_URL = "http://localhost:8003"
REDIS_HOST = "localhost"
REDIS_PORT = 6379
TTS_INPUT_QUEUE_KEY = "tts_input_queue"
TTS_OUTPUT_QUEUE_KEY = "tts_output_queue"

VOICE_ENGINE = pyttsx3.init()
VOICE_ENGINE.setProperty("rate", 150)
VOICE_ENGINE.setProperty("volume", 0.9)

#######################################################################
#    Redis 数据库配置
#######################################################################
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)


#######################################################################
#    Util 函数
#######################################################################
def parse_tts_data(asr_data: dict) -> dict:
    """解析Redis原始字节数据为可读字典"""
    parsed = {}
    for k, v in asr_data.items():
        key = k.decode("utf-8")
        try:
            # 特殊处理二进制字段
            if key == "audio":
                parsed[key] = f"<audio_data {len(v)} bytes: {v[:16].hex()}...>"
            else:
                parsed[key] = v.decode("utf-8")
        except Exception as e:
            parsed[key] = f"<decode_error: {str(e)}>"
    return parsed


#######################################################################
#    TTS 异步任务
#######################################################################


# 异步任务处理函数 将文本转为语音
async def process_tts_task(app: FastAPI, session_id: str):
    """
    处理文本转语音任务

    参数:
        app: FastAPI 应用实例, 用于获取Redis连接
        session_id: 会话唯一ID, 用于关联配置和生成结果

    处理流程:
        1. 从Redis 获取会陪配置和TTS数据
        2. 检查数据有效性 (状态, 文本)
        3. 创建临时文本文件保存生成的语音
        4. 调用TTS引擎进行语音合成
        5. 调整音频格式并更新结果到Redis
        6. 推送任务完成通知
        7. 清理临时资源
    """
    logger.info("开始处理 TTS 任务, session_id: %s", session_id)

    try:
        redis_conn = app.state.redis

        # 获取会话配置
        session_data = await redis_conn.hgetall(f"session:{session_id}")

        # 获取 TTS 条目数据
        tts_data = await redis_conn.hgetall(f"tts:{session_id}")

        if not tts_data or not session_data:
            logger.error("TTS 条目数据或会话数据不存在, session_id: %s", session_id)
            return

        # 状态检查
        if tts_data.get(b"status") == b"True":
            audio_bytes = tts_data.get(b"audio")
            if not audio_bytes:
                logger.error(
                    "状态为True但audio为空, 删除无效数据: %s 数据: %s",
                    session_id,
                    parse_tts_data,
                )
                await redis_conn.delete(f"tts:{session_id}")
                return

            logger.info("TTS 状态已为 True, 直接推送到输出队列 %s", session_id)
            await redis_conn.lpush(TTS_OUTPUT_QUEUE_KEY, session_id)

        # 获取待合成文本
        text = tts_data.get(b"text", b"").decode()
        if not text:
            logger.error(
                "TTS 文本数据为空, 无法进行语音合成, 删除无效数据: %s, 完整数据 : %s",
                session_id,
                parse_tts_data(tts_data),
            )
            await redis_conn.delete(f"tts:{session_id}")

        # 获取音频配置参数
        sample_rate = int(session_data.get(b"audio_sample", b"16000").decode())
        channels = int(session_data.get(b"audio_channel", b"1").decode())

        # 执行 TTS 转换 (再异步线程中运行同步的 pyttsx3)
        loop = asyncio.get_event_loop()

        global VOICE_ENGINE
        engine = VOICE_ENGINE
        # engine = pyttsx3.init()

        # # 配置语音参数
        # engine.setProperty("rate", 150)  # 语速
        # engine.setProperty("volume", 0.9)  # 音量

        # 创建临时文件
        tmp_path = None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            tmp_path = temp_file.name
            temp_file.close()  # 关闭文件， 方便其他进程访问

        # 异步执行TTS合成
        await loop.run_in_executor(None, lambda: engine.save_to_file(text, tmp_path))
        await loop.run_in_executor(None, engine.runAndWait)

        # 读取生成的音频文件
        audio = AudioSegment.from_wav(tmp_path)
        # 调整音频格式参数
        audio = audio.set_frame_rate(sample_rate).set_channels(channels)

        # 转换为字节流
        wav_buffer = io.BytesIO()
        audio.export(wav_buffer, format="wav")
        wav_data = wav_buffer.getvalue()

        # 更新Redis中的音频数据
        await redis_conn.hset(
            f"tts:{session_id}",
            mapping={"audio": wav_data, "status": "True"},
        )

        # 将完成的任务推送到输出队列
        await redis_conn.lpush(TTS_OUTPUT_QUEUE_KEY, session_id)
        logger.info(f"生成完成， 将session_id推送到 TTS 输出队列")

        # 清理临时文件
        os.unlink(tmp_path)

        logger.info(f"TTS 任务完成: {session_id}")

    except Exception as e:
        logger.error("TTS 任务失败: %s - %s", session_id, str(e), exc_info=True)
        await redis_conn.delete(f"tts:{session_id}")

    finally:
        # 清理临时文件
        if tmp_path:
            os.unlink(tmp_path)


#######################################################################
#    fastapi 接口
#######################################################################


# 新增 Redis 监听任务
async def redis_listener(app: FastAPI):
    """后台监听 Reids 任务队列"""
    while True:
        try:
            result = await app.state.redis.brpop(TTS_INPUT_QUEUE_KEY)
            if result:
                _, session_id = result
                str_session_id = session_id.decode()
                logger.info(
                    f"监听 TTS 输入队列 , 收到新的 TTS 任务, session_id: {str_session_id}"
                )
                asyncio.create_task(process_tts_task(app, str_session_id))

        except Exception as e:
            logger.error(f"Redis listener error: {str(e)}")
            await asyncio.sleep(1)


# 生命周期函数
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


# 初始化APP
app = FastAPI(lifespan=lifespan)


@app.post("/tts")
async def create_tts_item(session_id: str, text: str):
    pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )

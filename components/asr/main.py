import asyncio
from contextlib import asynccontextmanager
from funasr import AutoModel
import os
import logging
import redis.asyncio as redis
from fastapi import FastAPI
import tempfile
import torch

# 示例
# 识别结果: [{'key': 'test', 'text': '<|zh|><|NEUTRAL|><|BGM|><|woitn|>上一期的武林外传在大家的努力之下冲上了全战第一这个场面我真的从来没见过所以之后这一个月我就跟打了鸡血一样去做后院场景更新的承诺那么这期视频就要是我已经做到了同样是以大门视角把后院分为上'}]
#


#######################################################################
#    配置日志
#######################################################################

# 配置日志记录

logger = logging.getLogger("asr_server")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "asr_server.log")

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
UVICORN_HOST = "192.168.0.111"
UVICORN_PORT = 8004  # FastAPI服务监听端口

REDIS_HOST = "localhost"
REDIS_PORT = 6379
ASR_INPUT_QUEUE_KEY = "asr_input_queue"
ASR_OUTPUT_QUEUE_KEY = "asr_output_queue"

asr_engine = AutoModel(
    model="iic/SenseVoiceSmall",
    vad_kwargs={"max_silence_duration": 3000},
    disable_update=True,
    device="cuda:0" if torch.cuda.is_available() else "cpu",
    task="asr",  # 明确指定任务类型
)


#######################################################################
#    Util 函数
#######################################################################
def parse_asr_data(asr_data: dict) -> dict:
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
#    ASR 异步任务
#######################################################################


async def process_asr_task(app: FastAPI, session_id: str):
    """
    自动处理语音识别 (ASR) 任务

    参数:
        app: FastAPI应用实例, 用于获取Redis连接
        session_id: 会话唯一ID, 用于关联音频数据和识别结果

    返回:
        None

    处理流程:
        1. 从 Redis 获取指定 session_id 的音频数据
        2. 检查数据有效性 (状态, 音频内容)
        3. 创建临时文件保存音频数据
        4. 调用语音识别引擎进行识别
        5. 更新识别结果到Redis
        6. 推送任务完成通知
        7. 清理临时资源

    注意:
        - 音频文件处理时保证音频文件资源释放

    """
    logger.info("开始处理 ASR 任务, session_id: %s", session_id)
    tmp_path = None

    try:
        redis_conn = app.state.redis

        # 获取ASR条目数据
        asr_data = await redis_conn.hgetall(f"asr:{session_id}")
        if not asr_data:
            logger.error("ASR 数据不存在, session_id: %s", session_id)

        # 状态检查
        if asr_data.get(b"status") == b"True":
            text = asr_data.get(b"text")
            if not text:
                logger.error(
                    "ASR 状态已为 True 但text为空 , 无法推送到输出队列, 删除无效数据 %s, 完整数据: %s",
                    session_id,
                    parse_asr_data(asr_data),
                )
                await redis_conn.delete(f"asr:{session_id}")
                return
            logger.info("ASR 状态已为 True, 直接推送到输出队列 %s", session_id)
            await redis_conn.lpush(ASR_OUTPUT_QUEUE_KEY, session_id)

        # 读取音频字节数据
        audio_bytes = asr_data.get(b"audio", b"")
        if not audio_bytes:
            logger.error(
                "ASR 音频数据为空 , 无法进行音频识别 ,  删除无效数据 : %s, 完整数据 : %s",
                session_id,
                parse_asr_data(asr_data),
            )
            await redis_conn.delete(f"asr:{session_id}")
            return

        # 创建临时文件 (使用异步执行)
        loop = asyncio.get_event_loop()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            tmp_path = temp_file.name
            await loop.run_in_executor(None, temp_file.write, audio_bytes)

        # 异步执行语音识别
        result = await loop.run_in_executor(None, asr_engine.generate, tmp_path)
        text = result[0]["text"] if result else ""

        # 更新识别结果到 Redis
        await redis_conn.hset(
            f"asr:{session_id}",
            mapping={"text": text, "status": "True"},
        )

        # 推送至输出队列
        await redis_conn.lpush(ASR_OUTPUT_QUEUE_KEY, session_id)
        logger.info(f"ASR任务完成: {session_id}")

    except asyncio.CancelledError:
        logger.warning("ASR 任务被取消: %s", session_id)
        raise
    except Exception as e:
        logger.error("ASR 任务失败 : %s - %s", session_id, str(e), exc_info=True)
        await redis_conn.delete(f"asr:{session_id}")
    finally:
        # 清理临时文件
        if tmp_path:
            os.unlink(tmp_path)


#######################################################################
#    fastapi 接口
#######################################################################


# 新增 Redis 监听任务
async def redis_listener(app: FastAPI):
    """后台监听 Redis 任务队列"""
    while True:
        try:
            result = await app.state.redis.brpop(ASR_INPUT_QUEUE_KEY, timeout=0)

            if result:
                _, session_id = result
                str_session_id = session_id.decode()
                logger.info(
                    f"监听 ASR 输入队列 , 收到新的任务, session_id: {str_session_id}"
                )
                asyncio.create_task(process_asr_task(app, str_session_id))

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


# 初始化 APP
app = FastAPI(lifespan=lifespan)


@app.post("/asr")
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

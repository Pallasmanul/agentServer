"""
音频通讯模块


模块功能
1. UDP音频通信管理
    - 多会话UDP通道创建/销毁    注: 每个UDP通道对应一个设备, TODO: 根据设备UDP音频类型,让一个UDP通道对应多个设备

2. 音频数据处理
   - Opus编解码
   - AES-CTR模式加密
   - 语音活动检测(VAD)

主要组件：
- UdpProtocol    : UDP协议实现,处理数据接收/发送生命周期
- UDP线程池管理   : 管理多个并发的UDP会话通道
- 音频发送        : TTS队列->PCM->Opus->AES加密->网络传输
- 音频接收        : 网络传输->AES解密->Opus解码->PCM->ASR队列
"""

from contextlib import asynccontextmanager
import os
import logging

from fastapi import FastAPI, HTTPException, Query

import opuslib_next
import secrets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

import socket

import asyncio
import redis.asyncio as redis
import webrtcvad

import wave
from io import BytesIO

import aiohttp

from pydantic import BaseModel, Field

#######################################################################
#    配置日志
#######################################################################

# 配置日志记录

logger = logging.getLogger("audio_io")
logger.setLevel(logging.INFO)

# 日志目录
current_dir = os.getcwd()
log_file_path = os.path.join(current_dir, "storage/logs")
log_dir = os.path.expanduser(log_file_path)
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "audio_io.log")

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
# UDP 池
UVICORN_HOST = "192.168.0.111"  # FastAPI服务监听地址
UVICORN_PORT = 8001  # FastAPI服务监听端口
UDP_ADDRESS = "192.168.0.111"  # UDP服务默认监听地址
UDP_PORT = 5000  # UDP服务默认监听端口

udp_pool = {}


# DAO 服务地址
DAO_ASR_URL = "http://192.168.0.111:8005/asr"
DAO_SESSION_URL = "http://192.168.0.111:8005/sessions"


# REDIS 服务地址
REDIS_HOST = "localhost"
REDIS_PORT = 6379

# REDIS TTS队列
TTS_OUTPUT_QUEUE_KEY = "tts_output_queue"
#######################################################################
#    API 函数
#######################################################################


async def submit_to_asr_queue(session_id: str, audio_data: bytes):
    """通过DAO接口创建ASR条目"""
    try:
        async with aiohttp.ClientSession() as session:
            form_data = aiohttp.FormData()
            form_data.add_field(
                "audio",
                audio_data,
                content_type="audio/wav",
                filename=f"{session_id}.wav",
            )
            async with session.post(
                DAO_ASR_URL, data=form_data, params={"session_id": session_id}
            ) as response:
                if response.status == 200:
                    logger.info(f"ASR条目创建成功 session:{session_id}")
                else:
                    logger.error(f"ASR创建失败: {await response.text()}")

    except Exception as e:
        logger.error(f"创建ASR条目异常: {str(e)}")


async def create_session(
    session_id: str,
    client_id: str,
    tts_role: str,
    username: str,
    role: str,
) -> bool:
    """通过DAO接口创建session"""
    try:
        async with aiohttp.ClientSession() as session:
            data = {
                "session_id": session_id,
                "client_id": client_id,
                "username": username,
                "tts_role": tts_role,
                "role": role,
            }

            async with session.post(DAO_SESSION_URL, json=data) as response:
                if response.status == 201:
                    logging.info(f"会话创建成功 session:{session_id}")
                    return True
                logger.error(f"会话创建失败: {await response.text()}")
                return False

    except aiohttp.ClientError as e:
        logger.error(f"DAO接口网络异常: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"未知错误: {str(e)}")
        return False


#######################################################################
#    编解码函数 每个线程的编解码参数不完全一样, 故由每个线程单独创建编解码器，独立编解码
#######################################################################


#######################################################################
#    数据解析 加密/解密
#######################################################################


# 数据加密函数
def encrypt_audio_data(key, nonce, audio_data, udp_sequence):
    """
    使用AES-CTR模式加密音频数据并构造协议包

    参数:
        key (bytes): 16字节AES加密密钥
        nonce (bytes): 16字节初始向量 (包含协议头/长度/序列号信息)
        audio_data (butes): 原始音频数据
        udp_sequence (int): 当前序列号, 用于防止重放攻击

    返回:
        bytes: 完成数据包, 结构为 [新nonce(16字节)][加密后的数据]

    处理流程:
        1. 初始化AES-CTR加密器
        2. 加密原始音频数据
        3. 准备协议头信息并构造新nonce
        4. 组合最终数据包
    """
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    encryptor = cipher.encryptor()
    encryptor_audio = encryptor.update(audio_data) + encryptor.finalize()

    # 使用htons 和 htonl 将数据转换为网络字节序
    size = socket.htons(len(encryptor_audio))
    current_sequence = socket.htonl(udp_sequence + 1)

    size_bytes = size.to_bytes(2, "big")
    sequence_bytes = current_sequence.to_bytes(4, "big")

    nonce_list = list(nonce)
    # 第 0 字节填充headerr
    nonce_list[0] = 0x00
    # 第 2， 3 字节填充size
    nonce_list[2:4] = size_bytes
    # 第 12 - 15 字节填充sequence
    nonce_list[12:16] = sequence_bytes
    # 转换为bytes类型
    new_nonce = bytes(nonce_list)

    return new_nonce + encryptor_audio


# 数据解密函数
def decrypt_audio_data(key, received_data, udp_sequence):
    """
    解密接收的音频数据包并验证协议完整性

    参数:
        key (bytes): 16字节AES解密密钥
        received_data (bytes): 接收的完整数据包
        udp_sequence (int): 当前预期序列号

    返回:
        tuple: (解密后的音频数据, 实际接收序列号)

    处理流程:
        1. 验证数据包基础完整性
        2. 解析协议头信息
        3. 验证序列号连续性
        4. 执行AES-CTR解密
        5. 返回解密结果与验证后的序列号
    """
    if len(received_data) < 16:  # 至少包含 nonce
        logger.error("Received packet size is too small")
        return None, None
    nonce = received_data[:16]
    # 检查 header ， 值应该为 0x01
    if nonce[0] == 0x01:
        logger.error("Received packet type is incoorect")
        return None, None

    # 提取 size 并转换回主机字节序
    size_bytes = nonce[2:4]
    size = socket.ntohs(int.from_bytes(size_bytes, "big"))
    # 提取 seqence 并转换回主机字节序
    sequence_bytes = nonce[12:16]
    received_sequence = socket.ntohl(int.from_bytes(sequence_bytes, "big"))

    # 检验 received_sequence 是否为当前sequence 加 1
    if received_sequence != udp_sequence + 1:
        logger.error(f"Received sequence {received_sequence}")
        return None, None

    encrypted_audio = received_data[16 : 16 + size]
    if len(encrypted_audio) != size:
        logger.error("Actual encrypted audio size does not match the header size")
        return None, None

    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_audio = decryptor.update(encrypted_audio) + decryptor.finalize()

    return decrypted_audio, received_sequence


#######################################################################
#    音频数据处理函数
#######################################################################


def encode_audio(opus_encoder, input_frame_size, channels, sample_rate, data):
    """
    对原始PCM数据进行 Opus 编码

    参数:
        opus_encoder (opuslib_next.Encoder): 已初始化的Opus编码器实例
        input_frame_size (int): 音频帧时长 (单位: 毫秒)
        channels (int): 音频通道数量 (1-单声道, 2-立体声)
        sample_rate (int): 音频采样率 (如: 16000, 48000)
        data (bytes): 要编码的原始PCM数据, 16位有符号格式

    返回:
        list: 包含多个Opus编码帧的列表, 每个帧为bytes类型

    处理流程:
        1. 根据帧时长计算每帧样本数 (frame_samples)
        2. 计算每帧字节数 (frame_samples * 通道数 * 字节)
        3. 将输入数据按帧分割
        4. 对最后一帧不足部分进行零填充
        5. 逐帧进行Opus编码
    """
    opus_frames = []
    frame_samples = int(
        sample_rate * input_frame_size / 1000
    )  # 例如30ms 对应 480 samples @16Khz
    frame_bytes = frame_samples * channels * 2

    total_frames = (len(data) + frame_bytes - 1) // frame_bytes
    logger.info(
        "Begin Opus encode | Total frames: %d | Frame size: %d bytes",
        total_frames,
        frame_bytes,
    )

    for i in range(0, len(data), frame_bytes):
        chunk = data[i : i + frame_bytes]
        # 填充最后一帧
        if len(chunk) < frame_bytes:
            padding_size = frame_bytes - len(chunk)
            logger.debug("填充最后一帧, 添加 %d 字节", padding_size)
            chunk += b"\x00" * padding_size

        # 编码时使用样本数作为帧大小参数
        encoded = opus_encoder.encode(chunk, frame_samples)
        opus_frames.append(encoded)

    return opus_frames


def pcm_to_wav(sample_rate, channels, audio_data):
    """将PCM数据转为WAV格式"""
    with BytesIO() as wav_buffer:
        with wave.open(wav_buffer, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(
                2
            )  # 16位PCM , 目前不支持修改 TODO: 通过和设备协议,可设置PCM位宽
            wav.setframerate(sample_rate)
            wav.writeframes(audio_data)
    return wav_buffer.getvalue()


def wav_to_pcm(wav_data: bytes) -> tuple[int, int, bytes]:
    """从WAV数据中提取PCM音频参数和原始参数"""
    with BytesIO(wav_data) as wav_buffer:
        with wave.open(wav_buffer) as wav:
            if wav.getsamplewidth() != 2:
                raise ValueError("仅支持 16 位 PCM 格式的 WAV 文件")

            # 获取音频参数
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()

            # 读取全部PCM帧
            pcm_data = wav.readframes(wav.getnframes())

    return sample_rate, channels, pcm_data


def audio_vad(udp_protocol, session_id, data):
    """
    语言活动检测 (VAD) 处理函数

    参数:
        udp_protocol (UdpProtocol): UDP协议对象, 包含以下属性:
            - sample_rate (int): 音频采样率 (单位 :)
            - frame_duration(int) : 音频帧时长 (单位 ms)
            - frame_size (int): 单帧字节数 (自动计算)
            - vad (webrtcvad.Vad): VAD检测实例
            - speech_count (int): 连续语音帧计数器
            - slience_count (int): 连续静音帧计数器
            - audio_buffer (list): 音频数据缓冲区

        session_id (str): 当前会话的唯一标识
        data (bytes): PCM数据

    处理流程:
    1. 【参数准备】 从协议对象获取参数， 采样率，帧时长等
    2. 【帧分割】  按帧大小分割音频数据
    3. 【VAD检测】 逐帧进行语音/静音检测:
        - 检测到语音: 重置静音计数器, 累加语音计数器, 缓存音频
        - 检测到静音: 累加静音计数器, 若之前有语音则缓存尾音
    4. 【静音超时处理】:
        a. 短静音 (>1秒):
            - 将缓冲区的音频转为WAV格式
            - 异步提交到ASR服务
            - 重置缓冲区和计数器
        b. 长静音 (>10秒):
            - 记录超时日志
            - 异步关闭UDP通道
            - 终止后续处理
    """

    # 从协议获取对象参数
    sample_rate = udp_protocol.sample_rate
    frame_duration = udp_protocol.frame_duration
    frame_size = udp_protocol.frame_size
    vad = udp_protocol.vad

    # 处理音频帧
    for i in range(0, len(data), frame_size):
        frame = data[i : i + frame_size]
        if len(frame) < frame_size:
            continue

        is_speech = vad.is_speech(frame, sample_rate)

        if is_speech:
            udp_protocol.speech_count += 1
            udp_protocol.slience_count = 0
            udp_protocol.audio_buffer.append(frame)
        else:
            udp_protocol.slience_count += 1
            if udp_protocol.speech_count > 0:  # 缓冲尾音
                udp_protocol.audio_buffer.append(frame)

        # 检测 1秒 静音 ( 假设帧时长30ms, 约33帧为1秒)
        if udp_protocol.slience_count * frame_duration >= 1000:
            if udp_protocol.audio_buffer:
                # 打包 WAV 并上传 ASR
                wav_data = pcm_to_wav(
                    sample_rate=sample_rate,
                    channels=udp_pool[session_id]["channels"],
                    audio_data=b"".join(udp_protocol.audio_buffer),
                )

                # 异步提交到 ASR 队列
                asyncio.create_task(
                    submit_to_asr_queue(
                        session_id=udp_protocol.session_id, audio_data=wav_data
                    )
                )

                # 重置缓冲区
                udp_protocol.audio_buffer = []
                udp_protocol.speech_count = 0

        if udp_protocol.slience_count * frame_duration >= 10000:
            logger.info(
                f"Session {session_id} 因静音时间太长超时了,将发送goodbye帧,并清除响应会话和通道"
            )
            # 发送结束帧

            # 关闭通道
            asyncio.create_task(delete_udp_channel(session_id))
            return


#######################################################################
#    音频数据收发函数
#######################################################################


# 接收音频数据
def audio_receive_callback(udp_protocol, sequence, data):
    logger.info(
        f"Received audio data for session {udp_protocol.session_id}, sequence: {sequence}, data length: {len(data)}"
    )
    audio_vad(udp_protocol, udp_protocol.session_id, data)


# 发送音频数据
async def send_audio_data(session_id, audio_data):
    """
    通过指定会话的UDP通道发送音频数据

    参数:
        session_id (str): 要发送的会话ID
        audio_data (bytes): 要发送的原始PCM音频数据, 要求为16位有效符号格式

    流程说明:
        1. 验证会话有效性
        2. 获取该会话的加密参数和编码器
        3. 使用opus编码器压缩音频数据
        4. 使用AES-CTR模式加密数据包
        5. 通过UDP协议发送加密后的数据

    示例:
        >>> await send_audio_data("session_id", pcm_data)

    注意:

    """
    if session_id not in udp_pool:
        logger.error(f"session {session_id} not found in UDP pool")
        return

    transport = udp_pool[session_id]["transport"]
    protocol = udp_pool[session_id]["protocol"]
    key = udp_pool[session_id]["key"]
    nonce = udp_pool[session_id]["nonce"]
    opus = udp_pool[session_id]["opus_encoder"]
    channels = udp_pool[session_id]["channels"]
    sample_rate = udp_pool[session_id]["input_sample_rate"]
    frame_duration = udp_pool[session_id]["frame_duration"]

    # input frame size

    # opus 编码
    opus_encoded_frames = encode_audio(
        opus, frame_duration, channels, sample_rate, audio_data
    )

    # 加密封包
    encrypted_data = encrypt_audio_data(
        key, nonce, opus_encoded_frames, protocol.sequence
    )

    # 发送
    transport.sendto(encrypted_data)


#######################################################################
#    UDP线程
#######################################################################


class UdpProtocol(asyncio.DatagramProtocol):
    """
    UDP 协议处理类, 负责音频数据的接收 , 解密 和语音活动检测

    参数:
        session_id (str): 唯一会话标识符
        on_receive (callable): 数据接收回调函数
        input_sample_rate (int): 音频采样率 (如16000)
        channels (int): 音频通道数 (1-单声道, 2-立体声)
        frame_duration (int): 音频帧时长 (毫秒)


    """

    def __init__(
        self, session_id, on_receive, input_sample_rate, channels, frame_duration
    ):
        self.transport = None
        self.session_id = session_id  # 用于获取线程池参数，发送ASR条目
        self.on_received = on_receive  # UDP接收回调函数
        self.sequence = 0  # 当前UDP线程的 sequence

        # 初始化 opus 编码器
        self.opus_encoder = opuslib_next.Encoder(
            input_sample_rate, channels, opuslib_next.APPLICATION_VOIP
        )

        # 初始化VAD检测
        self.vad = webrtcvad.Vad()
        self.vad.set_mode(3)
        self.sample_rate = input_sample_rate
        self.frame_duration = frame_duration
        self.frame_size = int(input_sample_rate * frame_duration / 1000) * 2
        self.speech_count = 0
        self.slience_count = 0
        self.audio_buffer = []

    def connection_made(self, transport):
        self.transport = transport
        logger.info(f"UDP channel created for session {self.session_id}")

    def datagram_received(self, data, addr):
        """
        处理接收到的UDP数据
        处理流程:
        1. 数据包解密验证
        2. 序列号连续性检查
        3. Opus音频解码
        4. 调用回调函数

        参数:
            data(bytes): 原始加密的音频数据包
            addr: 来源地址 (host, port)元组
        """
        logger.info(
            f"Received {len(data)} bytes from {addr} for session {self.session_id}"
        )
        key = udp_pool[self.session_id]["key"]

        # 解密音频数据
        decrypted_audio, received_sequence = decrypt_audio_data(key, data, self.sequence)
        if decrypted_audio is None:
            return

        # 更新当前 sequence
        udp_pool[self.session_id]["sequence"] = received_sequence

        # opus 解码
        try:
            pcm_decode_data = self.opus_encoder.decode(decrypted_audio)
        except Exception as e:
            logger.error(f"failed to decode audio for session {self.session_id}: {e}")
            pcm_decode_data = None

        # 进入接收回调函数
        if pcm_decode_data:
            self.on_received(self, received_sequence, pcm_decode_data)

    def error_received(self, exc):
        logger.error(f"Error received for session {self.session_id}: {exc}")
        return super().error_received(exc)

    def connection_lost(self, exc):
        logger.error(f"Connection closed for session {self.session_id}")
        if self.session_id in udp_pool:
            del udp_pool[self.session_id]
        return super().connection_lost(exc)


#######################################################################
#    UDP线程池管理函数
#######################################################################


# 异步创建UDP通道
async def create_udp_channel(session_id, input_sample_rate, channels, frame_duration):
    """
    创建并初始化UDP音频传输通道

    参数:
        session_id (str): 唯一会话ID
        input_sample_rate (int): 音频输入采样率 (如: 16000, 48000)
        channels (int): 音频通道数 (1-单声道 , 2-立体声)
        frame_duration (int): 音频帧时长 (单位: 毫秒)

    返回:
        dict: 包含通道信息的字典, 结构为:
        {
            "message" 操作结果描述,
            "udp_address": UDP服务地址,
            "udp_port": UDP服务端口
        }

    示例:
        >>> await create_udp_channel("session_id", 16000, 1, 30)
    """
    loop = asyncio.get_running_loop()

    # 生成 16 字节的AES密钥
    key = secrets.token_bytes(16)
    # 生成 16 字节的nonce
    nonce = secrets.token_bytes(16)
    # 使用0端口让系统自动分配可用端口
    udp_address = (UDP_ADDRESS, 0)
    # 初始化 opus 编码器 , 用于udp发送数据的时候编码
    opus_encoder = opuslib_next.Encoder(
        input_sample_rate, channels, opuslib_next.APPLICATION_VOIP
    )

    def protocol_factory():
        return UdpProtocol(
            session_id,
            audio_receive_callback,
            input_sample_rate,
            channels,
            frame_duration,
        )

    transport, protocol = await loop.create_datagram_endpoint(
        protocol_factory,
        local_addr=udp_address,
    )

    # 获取绑定的UDP地址和自动分配的端口号
    sockname = transport.get_extra_info("sockname")

    udp_pool[session_id] = {
        "transport": transport,
        "protocol": protocol,
        "key": key,
        "nonce": nonce,
        "opus_encoder": opus_encoder,
        "input_sample_rate": input_sample_rate,
        "channels": channels,
        "frame_duration": frame_duration,
    }

    return {
        "message": f"UDP channel created for session {session_id}",
        "udp_address": sockname[0],
        "udp_port": sockname[1],
        "key": key.hex(),  # 转换为十六进制字符串
        "nonce": nonce.hex(),
    }


async def delete_udp_channel(session_id):
    """
    关闭并删除指定会话的UDP通道

    参数:
        session_id (str): 要关闭的会话标识符

    处理流程:
        1. 检查会话是否存在
        2. 关闭网络传输通道
        3. 从连接池移除会话记录
        4. 记录操作日志

    注意:
        当会话不存在时会记录错误日志但不会抛出异常
    """
    if session_id in udp_pool:
        transport = udp_pool[session_id]["transport"]
        transport.close()
        del udp_pool[session_id]

        logger.info(f"UDP channel for session {session_id} has been deleted.")
    else:
        logger.error(
            f"Session {session_id} not found in UDP pool, cannot delete. please check udp pool"
        )


#######################################################################
#    TTS 音频输出任务
#######################################################################


async def process_tts_audio(app: FastAPI, session_id: str):
    """
    从Redis获取指定会话的TTS音频数据, 通过UDP通道发送到客户端

    参数:
        app (FastAPI): FastAPI应用实例
        session_id (str): 需要处理的会话唯一ID

    返回:
        None

    处理流程:
        1. 从Redis获取TTS音频数据
        2. 验证音频数据完整性
        3. 通过UDP通道发送音频数据
        4. 清理Redis中的临时数据

    """
    logger.info("开始处理TTS音频输出任务, session_id: %s", session_id)

    try:
        redis_conn = app.state.redis

        # 获取TTS音频数据
        tts_data = await redis_conn.hgetall(f"tts:{session_id}")
        if not tts_data:
            logger.error("TTS音频数据不存在, session_id: %s", session_id)
            return

        # 获取音频数据
        audio_bytes = tts_data.get(b"audio")
        if not audio_bytes:
            logger.error("音频数据为空, session_id: %s", session_id)
            return

        # 转换 WAV 为 PCM
        sample_rate, channels, pcm_data = wav_to_pcm(audio_bytes)

        # 发送加密音频
        await send_audio_data(session_id, pcm_data)

        # 通过对应的UDP通道发送消息
        if session_id in udp_pool:
            await send_audio_data(session_id, audio_bytes)
            logger.info(f"已发送TTS音频数据到客户端, session_id: {session_id}")

        await redis_conn.delete(f"tts:{session_id}")

    except Exception as e:
        logger.error("处理TTS音频失败 %s - %s", session_id, str(e), exc_info=True)
        if tts_data:
            await redis_conn.delete(f"tts:{session_id}")


#######################################################################
#    请求数据
#######################################################################


class AudioChannelBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    input_sample_rate: int = Field(..., description="音频采样率")
    channels: int = Field(..., description="通道数量")
    frame_duration: int = Field(..., description="帧大小")


#######################################################################
#    fastapi 接口
#######################################################################


# Reids 监听任务
async def redis_listener(app: FastAPI):
    """监听 Redis 任务队列"""
    while True:
        try:
            result = await app.state.redis.brpop(TTS_OUTPUT_QUEUE_KEY)
            if result:
                _, session_id = result
                str_session_id = session_id.decode()
                logger.info(f"监听到 TTS 输出队列, session_id: {str_session_id}")
                asyncio.create_task(process_tts_audio(app, str_session_id))

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
        # 创建后台监听任务
        app.state.redis_listener = asyncio.create_task(redis_listener(app))
    except Exception as e:
        logger.error("无法连接到 Redis 服务器 %s", e)
        raise

    yield

    await app.state.redis.close()
    app.state.redis_listener.cancel()
    try:
        await app.state.redis_listener
    except asyncio.CancelledError:
        logger.info("Redis listener task 已取消")
    except Exception as e:
        logger.error("Redis listener 异常终止: %s", e)


# 创建FaspApi服务器
app = FastAPI(lifespan=lifespan)


@app.post("/udp_channel")
async def api_create_udp_channel(audio_base: AudioChannelBase):
    try:
        result = await create_udp_channel(
            audio_base.session_id,
            audio_base.input_sample_rate,
            audio_base.channels,
            audio_base.frame_duration,
        )
        logger.info("UDP通道创建成功: session_id: {audio_base.session_id}")
        return result

    except Exception as e:
        logger.error(
            f"Failed to create UDP channel for session {audio_base.session_id}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/udp_channel/{session_id}")
async def api_delete_udp_channel(session_id: str):
    try:
        await delete_udp_channel(session_id)

    except Exception as e:
        logger.error(f"Failed to delete UDP channel for session {session_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/udp_pool")
async def api_get_udp_pool(session_id: str = Query(..., description="要查询的session_id")):
    """获取当前UDP线程池状态"""
    try:
        if session_id:
            if session_id not in udp_pool:
                return {"error": f"Session {session_id} not found"}

            session_data = udp_pool[session_id]
            transport = session_data["transport"]
            sockname = transport.get_extra_info("sockname")

            return {
                "session_id": session_id,
                "udp_address": sockname[0],
                "udp_port": sockname[1],
                "input_sample_rate": session_data["input_sample_rate"],
                "channels": session_data["channels"],
                "frame_duration": session_data["frame_duration"],
                "last_sequence": session_data.get("sequence", 0),
                "nonce": session_data["nonce"].hex(),
                "key": session_data["key"].hex(),
            }
        else:
            pool_info = []
            for sid, data in udp_pool.items():
                transport = data["transport"]
                sockname = transport.get_extra_info("sockname")
                pool_info.append(
                    {
                        "session_id": sid,
                        "udp_address": sockname[0],
                        "udp_port": sockname[1],
                        "input_sample_rate": data["input_sample_rate"],
                        "channels": data["channels"],
                        "frame_sequence": data.get("sequence", 0),
                        "nonce": data["nonce"].hex(),
                        "key": data["key"].hex(),
                    }
                )

            return {"udp_channels": pool_info}

    except Exception as e:
        logger.error(f"获取线程池状态失败: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )

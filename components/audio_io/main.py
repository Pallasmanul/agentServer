import os
import logging
import asyncio
from fastapi import FastAPI, File, UploadFile
from concurrent.futures import ThreadPoolExecutor

from fastapi.responses import StreamingResponse
from datetime import datetime

import opuslib_next
import redis

import secrets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

import socket
import pyttsx3
import io
import soundfile as sf

import numpy as np

import tempfile
from pydub import AudioSegment


from funasr import AutoModel
from modelscope.hub.snapshot_download import snapshot_download

import webrtcvad

# 创建FaspApi服务器
app = FastAPI()


# 定义全局配置
audio_io_settings = {}

#######################################################################
#    配置日志
#######################################################################

# 日志目录
log_dir = os.path.expanduser("~/.log")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file_path = os.path.join(log_dir, "audio_process_server.log")


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(),
    ],
)

# 配置日志查看器
logger = logging.getLogger("audio_process_server")
# 开启日志
logger.setLevel(logging.INFO)
# 关闭日志
# logger.setLevel(logging.CRITICAL + 1)


#######################################################################
#    指令模板
#######################################################################


# 音频输出指令模板
# session       当前会话ID
# udp           udp地址
# role          角色，选择使用谁的音色
# emotion       情绪
# command         start 开始输出 ， get 获取状态,  stop 停止输出
# payload       文字
mqtt_message_audio_output_template = {
    "session": None,
    "udp": "",
    "role": "",
    "emotion": "",
    "command": "",
    "payload": "",
}


# 音频输出返回模板
# state          successful,  error, outputing
# message        successful or error message
mqtt_message_audio_output_return_template = {
    "session": None,
    "state": "",
    "message": "",
}


# 音频输入指令模板
# session        当前会话ID
# udp            udp地址
# timeout        允许无音频输入的最大时长
# command        start 开始输出， stop 停止输入, get 获取音频输入状态
mqtt_message_audio_input_template = {
    "session": None,
    "udp": "",
    "timeout": 5,
    "command": "",
}


# 音频输入返回模板
# session        当前会话ID
# role           角色， 用于识别是谁说的话， 这里预留
# emotion        情绪
# payload        文字
# time           音频持续时长 , 超过此事件，则返回信息，通知上层没有收到信息
# state          successful ,  error , inputing
# message        successful or error message
mqtt_message_audio_input_return_template = {
    "session": None,
    "role": "",
    "emotion": "",
    "payload": "",
    "time": 0,
    "state": "",
    "message": "",
}


#######################################################################
#    连接 Redis 数据库
#######################################################################
redis_client = redis.Redis(host="localhost", port=6379, db=0)
redis_pubsub = redis_client.pubsub()

# 定义订阅的频道
REDIS_CHANNEL_NAME = "audio_module_channel"
redis_pubsub.subscribe(REDIS_CHANNEL_NAME)


# 异步函数，用于持续监听 Redis 频道消息


async def listen_redis():
    while True:
        message = redis_pubsub.get_message()
        if message and message["type"] == "message":
            print(f"Received message: {message['data']}")

        await asyncio.sleep(0.1)


#######################################################################
#    编解码函数
#######################################################################


def encode_audio(data: bytearray):
    global opus_encoder
    if opus_encoder is None:
        print("init opus encoder first!")

    opus_frames = []
    frame_size = audio_io_settings.INPUT_FRAME_SIZE

    total_frames = len(data) + frame_size * 2 - 1
    print("begin opus encode , total frame %s", total_frames)
    for i in range(0, len(data), frame_size * 2):
        chunk = data[i : i + frame_size * 2]
        if len(chunk) < frame_size * 2:
            # 填充最后一帧
            padding_size = frame_size * 2 - len(chunk)
            logger.debug("填充最后一帧，添加%d字节", padding_size)
            chunk += b"\x00" * padding_size
        opus_frame = opus_encoder.encode(chunk, frame_size)
        opus_frames.append(opus_frame)

    return opus_frames


def decode_audio(opus_data):
    global opus_encoder
    if opus_encoder is None:
        print("init opus encoder first!")


#######################################################################
#    数据加密/解密
#######################################################################


# 数据加密函数
def encrypt_audio_data(key, nonce, audio_data, sequence):
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    encryptor = cipher.encryptor()
    encryptor_audio = encryptor.update(audio_data) + encryptor.finalize()

    # 使用htons 和 htonl 将数据转换为网络字节序
    size = socket.htons(len(encryptor_audio))
    current_sequence = socket.htonl(sequence + 1)

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
def decrypt_audio_data(key, received_data):
    if len(received_data) < 16:  # 至少包含 nonce
        logger.error("Received packet size is too small")
        return None, None, None
    nonce = received_data[:16]
    # 检查 header ， 值应该为 0x01
    if nonce[0] != 0x01:
        logger.error("Received packet type is incoorect")
        return None, None, None

    # 提取 size 并转换回主机字节序
    size_bytes = nonce[2:4]
    size = socket.ntohs(int.from_bytes(size_bytes, "big"))
    # 提取 seqence 并转换回主机字节序
    sequence_bytes = nonce[12:16]
    received_sequence = socket.ntohl(int.from_bytes(sequence_bytes, "big"))

    # 检验 received_sequence 是否为当前sequence 加 1
    if received_sequence != udp_sequence + 1:
        logger.error(f"Received sequence {received_sequence}")
        return None, None, None

    encrypted_audio = received_data[16 : 16 + size]
    if len(encrypted_audio) != size:
        logger.error("Actual encrypted audio size does not match the header size")
        return None, None, None

    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_audio = decryptor.update(encrypted_audio) + decryptor.finalize()

    return decrypted_audio, received_sequence


#######################################################################
#    文本转换为音频流
#######################################################################

# 配置tts , 设置语速 ， 音量 等信息
tts_engine = pyttsx3.init()
tts_engine.setProperty("rate", 150)
tts_engine.setProperty("volume", 1)
tts_voices = tts_engine.getProperty("voices")
tts_engine.setProperty("voice", tts_voices[0].id)


def tts_generate(text: str) -> bytes:
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_file:
            temp_file = temp_file.name
    except Exception as e:
        logger.error("generate tts error %s", e, exc_info=True)
        return None


def tts_config_audio(text: str):
    try:
        audio_data = tts_generate(text)
        if audio_data is None:
            print("无法生成TTS音频,终止转换过程")
            return None

        # 调整音频参数
        audio = AudioSegment.from_file(io.BytesIO(audio_data), format="wav")
        audio = audio.set_frame_rate(audio_io_settings.INPUT_SAMPLE_RATE)
        audio = audio.set_channels(audio_io_settings.CHANNELS)

        wav_data = io.BytesIO()
        audio.export(wav_data, format="wav")
        wav_data.seek(0)

        # 确保数据是 16 位帧数格式
        data, samplerate = sf.read(wav_data)
        if data.type != np.int16:
            data = (data * 32767).astype(np.int16)

        # 转换为字节序列
        raw_data = data.tobytes()

    except Exception as e:
        print("audio data transfrom false")
        pass


#######################################################################
#    音频转为文本流
#######################################################################

# # 配置 funcasr
# current_dir = os.getcwd()
# local_model_root = os.path.join(current_dir, "storage", "modules")
# modelscope_model_name = "iic/SenseVoiceSmall"
# # 生成以模型名称命名的文件夹
# model_folder_name = modelscope_model_name.split("/")[-1]
# local_model_path = os.path.join(local_model_root, model_folder_name)
# model_file_path = os.path.join(local_model_path, "model.pt")


# # 检查模型文件是否存在
# if not os.path.exists(model_file_path):
#     try:
#         print("本地模型文件不存在, 开始从 ModelScope 下载...")
#         # 创建模型文件夹
#         if not os.path.exists(local_model_path):
#             os.makedirs(local_model_path, exist_ok=True)

#         # 下载模型文件到本地的对应目录
#         snapshot_download(modelscope_model_name, cache_dir=local_model_path)
#         print(f"模型已下载到 {local_model_path}")
#     except Exception as e:
#         print(f"从 ModelScope 下载模型时失败失败: {e}")

# 初始化ASR引擎
asr_engine = AutoModel(
    model="iic/SenseVoiceSmall",
    vad_kwargs={"max_silence_duration": 3000},
    disable_update=True,
    device="cuda:0",
)


def asr_generate(audio_data: bytes) -> str:
    pass


#######################################################################
#    VAD 语音活动检测
#######################################################################


#######################################################################
#    音频数据接收回调函数
#######################################################################


def audio_receive_callback(data, session_id, sequence):
    logger.info(
        f"Received audio data for session {session_id}, sequence {sequence}, data length: {len(data)}"
    )


#######################################################################
#    音频数据接发送函数
#######################################################################
async def send_audio_data(session_id, audio_data):
    if session_id not in udp_pool:
        logger.error(f"Session {session_id} not found in UDP pool")
        return

    transport = udp_pool[session_id]["transport"]
    protocol = udp_pool[session_id]["protocol"]
    key = udp_pool[session_id]["key"]
    nonce = udp_pool[session_id]["nonce"]

    # Opus 编码
    opus_encoded_data = encode_audio(audio_data)
    # 加密
    encrypted_data = encrypt_audio_data(
        key, nonce, opus_encoded_data, protocol.sequence
    )
    # 发送
    transport.sendto(encrypted_data)


#######################################################################
#    UDP线程池
#######################################################################

# SEQUENCE
udp_sequence = 0

# UDP 池
udp_pool = {}


# UDP 协议类, 处理接收和发送数据
class UdpProtocol(asyncio.DatagramProtocol):
    def __init__(
        self, session_id, on_receive, input_sample_rate, channels, frame_duration
    ):
        self.transport = None
        self.session_id = session_id  # 用于获取当前UDP线程的参数
        self.on_receive = on_receive  # 当前UDP的回调函数
        self.sequence = 0  # 当前UDP线程的 sequence

        # 初始化 Opus 编码器
        self.opus_encoder = opuslib_next.Encoder(
            input_sample_rate, channels, opuslib_next.APPLICATION_VOIP
        )

        # 用于 vad 音频检测
        self.vad = webrtcvad.Vad()
        self.vad.set_mode(3)  # 设置VAD模式
        self.audio_buffer = []  # 用于接收每段对话
        self.silence_count = 0  # 帧内未检测到语音活动计数
        self.speech_count = 1  # 帧内检测到语音活动计数
        self.sample_rate = 16000  # 帧采样率
        self.frame_duration = 30  # 帧时长
        self.frame_size = (
            int(self.sample_rate * self.frame_duration / 1000) * 2
        )  # 每帧音频字节数
        self.temp_files = []  # 会话文件保存
        self.temp_file_index = 0

    def connection_made(self, transport):
        self.transport = transport
        print(f"UDP channel created for session {self.session_id}")

    def datagram_received(self, data, addr):
        print(f"Received {len(data)} bytes from {addr} for session {self.session_id}")
        if self.session_id not in udp_pool:
            logger.error(f"Session {self.session_id} not found in UDP pool")
            return
        key = udp_pool[self.session_id]["key"]

        # 解密音频数据
        decrypted_audio, received_sequence = decrypt_audio_data(key, data)
        if decrypted_audio is None:
            return

        # 更新当前 sequence
        udp_pool[self.session_id]["sequence"] = received_sequence

        # Opus 解码
        try:
            pcm_decode_data = self.opus_encoder.decode(decrypted_audio)
        except Exception as e:
            print(f"Failed to decode audio for session {self.session_id}: {e}")
            pcm_decode_data = None

        # 调用回调函数
        if pcm_decode_data:
            self.on_receive(pcm_decode_data, self.session_id, received_sequence)

        # 语音活动检测
        if pcm_decode_data:
            # 分割为适合的VAD帧  -- 设备传输的帧也是30ms ， 配置里设置了frame_size未见生效，可能未其他帧长度吧
            for i in range(0, len(pcm_decode_data), self.frame_size):
                frame = pcm_decode_data[i : i + self.frame_size]
                if len(frame) == self.frame_size:
                    is_speech = self.vad.is_speech(frame, self.sample_rate)
                    if is_speech:
                        self.speech_count += 1
                        self.silence_count = 0
                        self.audio_buffer.append(frame)
                    else:
                        self.silence_count += 1
                        if self.speech_count > 0:
                            self.audio_buffer.append(frame)

                # 检查是否连续1秒都没有检测到说话
                if (
                    self.speech_count > 0
                    and self.silence_count * self.frame_duration >= 1000
                ):
                    self.process_audio_to_text()  # 处理成文本发送给消息队列
                    self.speech_count = 0
                    self.audio_buffer = []

                # 检查是否 5 秒内都没有检测到说话
                if self.silence_count * self.frame_duration >= 5000:
                    self.transport.close()

    def save_audio_to_file(self):
        if self.audio_buffer:
            combined_data = b"".join(self.audio_buffer)
            temp_file_path = os.path.join(
                os.getcwd(), f"temp_{self.session_id}_{self.temp_file_index}.wav"
            )
            self.temp_files.append(temp_file_path)
            self.temp_file_index += 1
            try:
                data, _ = sf.read(io.BytesIO(combined_data), dtype="int16")
                sf.write(
                    temp_file_path,
                    data,
                    self.sample_rate,
                    channels=audio_io_settings.CHANNELS,
                )
                print(f"Audio data saved to {temp_file_path}")
            except Exception as e:
                logger.error(f"Failed to save audio data to file: {e}")

    def process_audio_to_text(self):
        if self.audio_buffer:
            combined_data = b"".join(self.audio_buffer)
            try:
                data, _ = sf.read(io.BytesIO(combined_data), dtype="int16")
                # 转换为 numpy 数组
                audio_np = np.array(data, dtype=np.float32)
                # 调用 funasr 进行语音转文本
                result = asr_engine.inference(audio_np, fs=self.sample_rate)
                # 获取文本 ， 返回的funasr可能还包含情绪，这里只获取文本数据
                text = result.get("text", "")
                print(text)

                # 如果识别到了文本
                if text:
                    # 向redis队列填充消息，消息会被控制端接收，然后传送给大模型识别
                    audio_to_llm_queue = redis_client.hget(
                        f"session:{self.session_id}", "audio_to_llm_queue"
                    )
                    if audio_to_llm_queue:
                        audio_to_llm_queue = audio_to_llm_queue.decode()
                        # 将识别后的文本放入队列
                        redis_client.rpush(audio_to_llm_queue, text)
                        print(f"Session {self.session_id} 识别结果已经放入消息队列")

                    print(f"Session {self.session_id} ASR result: {result}")
            except Exception as e:
                logger.error(
                    f"Failed to process audio to text for session {self.session_id}"
                )

    def error_received(self, exc):
        print(f"Error received for session {self.session_id}: {exc}")

    def connection_lost(self, exc):
        print(f"Connection closed for session {self.session_id}")
        if self.session_id in udp_pool:
            del udp_pool[self.session_id]


#######################################################################
#    UDP线程池管理函数
#######################################################################


# 异步创建 UDP 通道
async def create_udp_channel(
    udp_address, session_id, input_sample_rate, channels, frame_dration
):
    loop = asyncio.get_running_loop()

    # 生成16字节的AES密钥
    key = secrets.token_bytes(16)
    # 生成16字节的nonce
    nonce = secrets.token_bytes(16)

    def protocol_factory():
        return UdpProtocol(
            session_id,
            audio_receive_callback,
            input_sample_rate,
            channels,
            frame_dration,
        )

    if isinstance(udp_address, str):
        try:
            parts = udp_address.split(":")
            if len(parts) == 2:
                host = parts[0]
                port = int(parts[1])
                udp_address = (host, port)
            else:
                raise ValueError("Invalid udp_address format")
        except ValueError as e:
            print(f"Error parsing udp_address: {e}")
            return None, None, None

    transport, protocol = await loop.create_datagram_endpoint(
        protocol_factory,
        local_addr=udp_address,
    )
    udp_pool[session_id] = {
        "transport": transport,
        "protocol": protocol,
        "key": key,
        "nonce": nonce,
    }

    return transport, key, nonce


# 异步删除 UDP 通道
def delete_udp_channel(session_id):
    if session_id in udp_pool:
        transport = udp_pool[session_id]["transport"]
        # 关闭 trasnport
        transport.close()
        # 从udp_pool 中删除会话信息
        del udp_pool[session_id]
        logger.info(f"DUP channel for session {session_id} has been deleted.")
    else:
        logger.warning(f"Session {session_id} not found in UDP pool, cannot delete.")


#######################################################################
#    fastapi 接口
#######################################################################


# 创建udp通道
@app.post("/create_udp_channel")
async def api_create_udp_channel(
    session_id: str,
    udp_address: str,
    input_sample_rate: int = 16000,
    channels: int = 1,
    frame_dration: int = 30,
):
    print(type(session_id))
    print(udp_address)
    try:
        transport, key, nonce = await create_udp_channel(
            udp_address, "hello", 16000, 1, 30
        )
        return {
            "message": f"UDP channel created for session {session_id}",
            "key": key.hex(),
            "nonce": nonce.hex(),
        }
    except Exception as e:
        return {
            "message": f"Failed to create UDP channel for session {session_id}: {str(e)}",
            "error": str(e),
        }


# 删除 UDP 通道
@app.delete("/udp_channel/{session_id}")
async def remove_udp_channel(session_id: str):
    try:
        delete_udp_channel(session_id)
        return {
            "message": f"UDP channel for session {session_id} has been successfully deleted."
        }
    except Exception as e:
        return {
            "message": f"Failed to delete UDP channel for session {session_id}: {str(e)}",
            "error": str(e),
        }


# 查询 UDP 线程池
@app.get("/udp_channels")
async def get_udp_channels_info():
    channels_info = {}
    for session_id, channel_info in udp_pool.items():
        # 提取关键信息， 这里可以根据需求添加更多信息
        transport = channel_info["transport"]
        local_addr = transport.get_extra_info("sockname") if transport else None
        info = {
            "local_address": local_addr,
            "key": channel_info["key"].hex(),
            "nonce": channel_info["nonce"].hex(),
        }
        channels_info[session_id] = info

    # 返回通道数量和每个通道的信息
    return {"channel_count": len(udp_pool), "channels_info": channels_info}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)

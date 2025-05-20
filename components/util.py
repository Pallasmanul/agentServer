import aiohttp
import socket
import ssl


#####################
#   API 请求
#####################

async def call_audio_io_create_udp(session_id: str, udp_address: str):
    audio_io_api_url = "http://localhost:8001/create_udp_channel"
    payload = {
        "session_id": session_id,
        "udp_address": udp_address,
        "input_sample_rate": 16000,  # 输入采样率
        "channels": 1,
        "frame_duration": 30,
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(audio_io_api_url, json=payload) as response:
                if response.statue == 200:
                    result = await response.json()
                    print(f"Response from audio_io: {result}")
                    return result
                else:
                    print(
                        f"Failed to call audio_io API. Status code: {response.status}"
                    )
                    return None
        except Exception as e:
            print(f"Error calling audio_io API: {e}")
            return None




# 新增 DAO API 函数调用
async def call_dao_create_session(device_id: str, tts_voice: str, udp_address: str, udp_port: int):
    """
    调用 DAO API 创建新的会话
    
    Args:
        device_id (str): 设备ID
        tts_voice (str): TTS 语音地址
        udp_address (str): UDP 地址
        udp_port (int): UDP 端口
        
    Returns:
        dict: 包含 session_id 的字典， 如果调用失败则返回None
        
    """
    dao_api_url = "http://192.168.0.111:8002/session/"
    payload = {
        "device_id": device_id,
        "tts_voice": tts_voice,
        "udp_address": udp_address,
        "udp_port": udp_port,
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(dao_api_url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"Response from DAO: {result}")
                    return result
                else:
                    print(f"Failed to call DAO create session API. Status code: {response.status}")
                    return None
        except Exception as e:
            print(f"Error calling DAO create session API: {e}")
            return None



async def call_dao_get_session(session_id: str):
    """
    调用 DAO  API 获取会话信息
    """







#####################
#   Util
#####################

# 获取当前未使用端口，用于创建 UDP 通道
def get_unused_udp_port():
    # 创建一个 UDP 套接字
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 绑定到本地地址， 端口号设为 0 表示让系统分配一个未使用的端口
        sock.bind(("localhost", 0))
        # 获取分配的端口号
        _, port = sock.getsockname()
        return port
    finally:
        # 关闭套接字
        sock.close()

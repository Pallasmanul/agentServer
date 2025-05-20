from pydantic import BaseModel, Field


class SessionDataBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    device_id: str = Field(..., description="设备ID")
    tts_role: str = Field(..., description="TTS角色")
    udp_address: str = Field(..., description="UDP地址")
    udp_port: int = Field(..., description="UDP端口")
    audio_sample: int = Field(..., description="音频采样率")
    audio_channel: int = Field(..., description="音频通道数")


class TTSDataBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    text: str = Field(..., description="TTS文本")
    audio: bytes = Field(..., description="音频数据")


class ASRDataBase(BaseModel):
    session_id: str = Field(..., description="会话ID")
    text: str = Field(..., description="ASR文本")
    audio: bytes = Field(..., description="音频数据")

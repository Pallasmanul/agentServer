


Request Headers: {'user-agent': 'esp32-s3-touch-lcd-1.85/1.5.5', 'host': '192.168.0.111:8000', 'accept-language': 'zh-CN', 'client-id': '98e0d91c-7f4b-4ee8-bbd4-917a26e76586', 'content-type': 'application/json', 'device-id': 'cc:ba:97:04:92:94', 'content-length': '1080'}


# OTA HTTP协议
## 客户端→服务器

1. **向OTA地址发送POST请求
- 连接成功后，如果设备之前注册过对应的消息，设备会通过POST发送一条 JSON 消息，示例结构如下：  
   ```json
   {
     "user-agent": "esp32-s3-touch-lcd-1.85/1.5.5",
     "host": 192.168.0.111:8000,
     "accept-language": "zh-CN",
     "client-id": "****",
     "content-type": "application/json",
     "device-id": "cc:ba:97:04:92:94",
     "content-length": '1080',
   }

    ```json
    {'version': 2, 'language': 'zh-CN', 'flash_size': 16777216, 'minimum_free_heap_size': 8288524, 'mac_address': 'cc:ba:97:04:92:94', 'uuid': '98e0d91c-7f4b-4ee8-bbd4-917a26e76586', 'chip_model_name': 'esp32s3', 'chip_info': {'model': 9, 'cores': 2, 'revision': 2, 'features': 18}, 'application': {'name': 'xiaozhi', 'version': '1.5.5', 'compile_time': 'Apr 24 2025T16:11:20Z', 'idf_version': 'v5.3.2', 'elf_sha256': '5f0b4203acc84910b742a2da9fa3bfa51173d70813558c0ba2f7d058211669b4'}, 'partition_table': [{'label': 'nvs', 'type': 1, 'subtype': 2, 'address': 36864, 'size': 16384}, {'label': 'otadata', 'type': 1, 'subtype': 0, 'address': 53248, 'size': 8192}, {'label': 'phy_init', 'type': 1, 'subtype': 1, 'address': 61440, 'size': 4096}, {'label': 'model', 'type': 1, 'subtype': 130, 'address': 65536, 'size': 983040}, {'label': 'ota_0', 'type': 0, 'subtype': 16, 'address': 1048576, 'size': 6291456}, {'label': 'ota_1', 'type': 0, 'subtype': 17, 'address': 7340032, 'size': 6291456}], 'ota': {'label': 'ota_0'}, 'board': {'type': 'esp32-s3-touch-lcd-1.85', 'name': 'esp32-s3-touch-lcd-1.85', 'ssid': 'Yiyiwork', 'rssi': -50, 'channel': 5, 'ip': '192.168.0.13', 'mac': 'cc:ba:97:04:92:94'}}









- 连接成功后，
- 服务器检查该固件是否有新版本，如果有新版本，将新版本固件信息发送回设备，如果该版本已是最新版本，不处理
- 服务器检查该固件是否已经注册在设备数据库中，如果没注册过，则生成一个注册码，将注册码和MQTT信息发送回设备
- 将当前的系统时间打包成发送到设备







I 服务器查询设备数据库，检查是否有这个设备，如果有，则不发送配置，如果没有，返回注册配置



## 服务器->客户端
1. **服务器收到POST请求后，会返回一个JSON消息，示例结构如下：
   ```json
   {
        "activation": {
            "message": "请使用以下激活码激活设备",
            "code": "****"
        },
        "mqtt": {
            "endpoint": "mqtt.example.com",
            "client_id": "device_001",
            "username": "user123",
            "password": "pass456",
            "publich_topic": "device/001/status"
        },
        "server_time": {
            "timestamp": 1629103600,
            "timezone_offset":480
        },
        "firmware": {
            "version": "1.1.0",
            "url: "https://example.com/firmware.bin"
        }
    }

I 客户端首先检查是否有新版本，如果有新版本，则弹出提示信息，等待设备状态变为空闲，然后安排升级任务。 升级过程中会更新显示信息，关闭不必要的功能，最后启动并处理失败的情况
II 


2. **参考
        "endpoint": "mqtt.xiaozhi.me",
        "client_id": "GID_test@@@c0_bf_be_11_8b_91@@@undefined",  数据头 ， MAC地址 数据尾
        "username": "eyJpcCI6IjIxOC43Mi40MC4xNyJ9",  ip地址 Base64 编码字符串
        "password": "oWtJF62pm0Rzo+Zh9dBq+MRveJNDKcHxEn3b1Do3qPU=", 经过编码
        "publish_topic": "device-server",




# MQTT 协议
## 客户端→服务器
1. **向MQTT地址发送hello帧 申请UDP通道
- 发送成功后，
   ```json
   {
     "type": "hello",
     "version": 3,
     "transport": "udp",
     "audio_params": {
        "format": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration": 60,
     }
   }
2. **向MQTT地址发送goodbye帧, 客户端关闭音频通道时发送该消息
   ```json
   {
    "session": "<session_id>",
    "type": "goodbye",
   }


## 服务器→客户端
1. **服务器收到hello帧后，会回复UDP的地址，会话ID，示例结构如下：
   ```json
   {
     "type": "hello",
     "transport": "udp",
     "session_id": "<session_id>",
     "audio_params": {
         "sample_rate": 16000,
         "frame_duration": 60,
     },
     "udp": {
         "address": "<udp_address>",
         "port": "<udp_port>",
         "key": "<hex_key>",
         "nonce": "hex_nonce",
     }
   }

2. **服务器发送该消息，通知客户端关闭会话
{
    "type": "goodbye",
    "session_id": "<session_id>",
}

3. **服务器发送消息, 通知客户端 TTS音频数据 传输开始
{
    "type": "tts",
    "state": "start",
}

4. **服务器发送消息, 通知客户端 TTS音频数据 传输结束
{
    "type": "tts",
    "state": "stop",
}




# UDP 协议
## UDP数据包接收格式
+-----------------
16 bytes 随机数 用于AES-CTR加密后的音频数据

size     : 音频数据长度
sequence : 服务器序列号

字节偏移量  |0   |1|  2 |3|4|5|6|7|8|9|a|b|     c    |d|e|f|g|
特定数据    |0x01|x|size|x|x|x|x|x|x|x|x|x|sequence+1|x|x|x|x|


### UDP接收数据包流程
1. 检查接收到的数据包大小是否合法
2. 检查数据包类型是否正确 (固定值 0x01)
3. 检查数据包类型是否正确


## UDP数据包发送格式
+-----------------
16 bytes 随机数 用于AES-CTR加密后的音频数据

size     : 音频数据长度
sequence : 设备序列号

字节偏移量  |0   |1|  2 |3|4|5|6|7|8|9|a|b|     c    |d|e|f|g|
特定数据    |0x00|x|size|x|x|x|x|x|x|x|x|x|sequence+1|x|x|x|x|




# 数据库数据

1. **session_list   (Set 集合)
```json
{
    "session_id":xxx
}

2. **session会话数据 (Hash)
```json
{
    "session_id": {
        "tts_role":"saike",
        "llm_input": "text",       # 用于LLM对话的文本，      AUDIO->LLM模块
        "tts_output": "text",      # 用于audio_io tts输出 ， LLM模块->AUDIO_IO
        "ip": "192.168.1.9",       # 远程地址数据
        "audio": {                 # 可以根据音频数据选择合适的音频通道
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60
        }
    }
}


# 设备状态及流转

| KDeviceStateStarting        | 启动状态，后续更具网络情况变更状态
| KDeviceStateWifiFonciguring | 当设备进入配网模式，进入该状态
| KDeviceStateIdle            | 设备空闲时处于该状态, 许多操作回以该状态为起点
| KDeviceStateConnecting      | 用户触发或唤醒后，设备会尝试建立 websocket/mqtt 连接, 申请UDP通道
| KDeviceStateListening       | 设备成功建立连接后，设备开始录音并上传音频数据 , 每采集30ms的数据就开始上传
| KDeviceStateSpeaking        | 收到服务器TTS Start消息后，设备停止录音并播放接收到的音频
| KDeviceStateUpgrading       | 检测到有新版本需要升级时， 设备会进入该状态
| KDeviceStateActivating      | 设备激活过程中会处于该状态

## 设备流转总结
1 设备启动 KDeviceStateStarting -> KDeviceStateIdle 或 KDeviceStateWifiConfiguring
2 交互阶段 KDeviceStateIdle -> KDeviceStateConnecting -> KDeviceStateListening -> KDeviceStateSpeaking
3 升级阶段 KDeviceStateIdle -> KDeviceStateUpgrading
4 激活阶段 KDeviceStateIdle -> KDeviceStateActivating


# 服务器状态
## 设备接入流程
1 监听到设备申请UDP通道的MQTT请求
2 调用audio_io api创建udp通道，并将通道信息填入回复消息队列
3 等待相应UDP通道的数据接收完成并将语音数据转换为文字数据
4 将得到的文字数据提交给后端大模型获取大模型回复
5 将大模型回复的数据提交给 audio_io 发送
6 发送完成之后发送 goodbye 帧 ， 并关闭udp通道




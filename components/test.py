
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
import aiohttp
import redis.asyncio as redis


async def create_device_data(device_id: str, client_id: str, ip_address: str) -> bool:
    """调用DAO接口创建设备数据"""
    global DAO_URL

    try:
        async with aiohttp.ClientSession() as session:
            data = {
                "device_id": device_id,
                "client_id": client_id,
                "ip_address": ip_address,
                "username": "",  # 注册流程中填充用户名
            }

            async with session.post(DAO_URL, json=data) as response:
                if response.status == 200:
                    print(f"设备 {device_id} 创建成功")
                    return True
                print(f"设备创建失败: {await response.text()}")
                return False

    except aiohttp.ClientError as e:
        print(f"网络请求异常: {str(e)}")
        return False
    except Exception as e:
        print(f"未知错误: {str(e)}")
        return False
    


import asyncio

# 假设 create_device_data 函数已定义
async def main():
    # 替换为实际参数
    success = await create_device_data(
        device_id="device_123",
        client_id="client_abc",
        ip_address="192.168.1.1"
    )
    print(f"创建结果: {success}")

# 运行异步函数
asyncio.run(main())
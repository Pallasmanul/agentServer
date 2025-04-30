"""An example of calling text llm interfaces by OpenAI-compatible interface"""

from qwen_agent.llm import get_chat_model


def test():
    llm_cfg = {
        "model": "qwen2.5:3b",  # 替换为你实际运行的模型名称
        "model_server": "http://localhost:11434/v1",  # Ollama 的 OpenAI 兼容 API 地址，若修改端口需同步更新
        "api_key": "",  # 本地部署无需 API Key
        "generate_cfg": {
            "max_tokens": 2048,  # 可选参数，设置最大生成 token 数
            "temperature": 0.7,  # 控制输出随机性
        },
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "image_gen",
                "description": "AI painting (image generation) service, input text description and image resolution, and return the URL of the image drawn based on the text information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Detailed description of the desired content of the generated image, such as details of characters, environment, actions, etc., in English.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        }
    ]

    # Chat with text llm
    try:
        llm = get_chat_model(llm_cfg)
    except Exception as e:
        print(f"Failed to connect to the model service: {e}")

    messages = [{"role": "user", "content": "你是？"}]
    """
    llm.quick_chat_oai
        This is a temporary OpenAI-compatible interface that is encapsulated and may change at any time.
        It is mainly used for temporary interfaces and should not be overly dependent.
        - Only supports full streaming
        - The message is in dict format
        - Only supports text LLM
    """
    try:
        response = llm.chat("hello")
        print(response)
    except Exception as e:
        print(f"Failed to chat with llm : {e}")


if __name__ == "__main__":
    test()

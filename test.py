from qwen_agent import Agent

# 配置 Ollama 模型的相关信息
llm_config = {
    "model": "qwen2.5:3b",  # 替换为你实际运行的模型名称
    "model_server": "http://localhost:11434/v1",  # Ollama 的 OpenAI 兼容 API 地址，若修改端口需同步更新
    "api_key": "",  # 本地部署无需 API Key
    "generate_cfg": {
        "max_tokens": 2048,  # 可选参数，设置最大生成 token 数
        "temperature": 0.7  # 控制输出随机性
    }
}

# 初始化 Agent
agent = Agent(llm=llm_config)

# 提出问题
question = "请介绍一下人工智能的发展趋势。"
answer = agent.run(question)

# 输出结果
print("模型回答：", answer)
    
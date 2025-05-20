
import unittest
from main import BaseFnCallModel
from qwen_agent.llm.schema import Message, ContentItem




class TestQwenFnCall(unittest.TestCase):
    def setUp(self):
        # 配置指向本地 Ollama 服务
        self.cfg = {
            'model': 'qwen2.5:1.5b',
            'model_server': 'http://localhost:11434/v1',
            'api_key': 'ollama', # Ollama不需要真实API密钥
            'generate_cfg': {
                'fncall_prompt_type': 'qwen', # 指定函数调用提示模板
                'temperature': 0.3
            }
        }
        self.agent = BaseFnCallModel(self.cfg)
        
    
    def test_function_calling(self):
        # 测试函数调用流程
        messages = [
            Message(role='user', content='打开卧室的灯')
        ]
        
        # 定义灯具控制函数
        functions = [{
            "name": "light_control",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "action": {"type": "string", "enum": ["on", "off"]}
                }
            }
        }]
        
        # 执行函数调用
        response = self.agent._chat_with_functions(
            messages=messages,
            function=functions,
            stream=False,
            delta_stream=False,
            generate_cfg={'function_choice': 'auto'},
            lang='zh'
        )

        # 验证响应包含函数调用
        self.assertIsNotNone(response[0].function_call)
        print("函数调用测试通过， 响应: ", response)
        

if __name__ == '__main__':
    unittest.main()
    
    






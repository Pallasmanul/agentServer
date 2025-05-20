
from langchain_ollama import OllamaLLM
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate

llm = OllamaLLM(model="qwen2.5:1.5b")


messages = [
    SystemMessage(content="Translate the following from English into Italian."),
    HumanMessage("hi!"),
]


system_template = "Translate the following from English into {language}"
prompt_template = ChatPromptTemplate.from_messages(
    [("system", system_template), ("user", "{text}")]
)



prompt = prompt_template.invoke({"language": "Spanish", "text": "hi!"})
prompt.to_messages()

response = llm.invoke(prompt)
print(response)



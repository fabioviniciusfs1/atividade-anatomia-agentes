import inspect
import json
import os
import time

from openai import OpenAI, RateLimitError
from dotenv import load_dotenv
from pathlib import Path
from typing import Any, Dict, List, Tuple

load_dotenv()

openai_client = OpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
)
SYSTEM_PROMPT = """
You are a coding assistant whose goal it is to help us solve coding tasks.
You can perform actions by emitting a single command line in exactly this format, and nothing else on that line:

tool: NAME({{"arg": "value"}})

Do not use JSON function-calling, a <tool_call> tag, or any other structured tool-call format your training may default to.
The ONLY format the system running you understands is the plain text line above.

Available commands:

{tool_list_repr}

Example of a correct response when you want to read a file named 'notes.txt':
tool: read_file({{"filename": "notes.txt"}})

Use compact single-line JSON with double quotes. After receiving a tool_result(...) message, continue the task using the same format when another action is needed.
If no action is needed, respond in plain prose.
"""

YOU_COLOR = "\u001b[94m"
ASSISTANT_COLOR = "\u001b[93m"
RESET_COLOR = "\u001b[0m"

TRACE_FILE = "trace.txt"

def log_trace(text: str):
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def extract_thought(text: str) -> str:
    """Texto do modelo sem as linhas 'tool: ...' que o parser reconheceu.
    Linhas 'tool:' malformadas permanecem, para evidenciar falhas de parsing."""
    lines = [l for l in text.splitlines() if not extract_tool_invocations(l)]
    return "\n".join(lines).strip()

def resolve_abs_path(path_str: str) -> Path:
    """
    file.py -> /Users/home/mihail/modern-software-dev-lectures/file.py
    """
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path

def read_file_tool(filename: str) -> Dict[str, Any]:
    """
    Gets the full content of a file provided by the user.
    :param filename: The name of the file to read.
    :return: The full content of the file.
    """
    full_path = resolve_abs_path(filename)
    print(full_path)
    with open(str(full_path), "r") as f:
        content = f.read()
    return {
        "file_path": str(full_path),
        "content": content
    }

def list_files_tool(path: str) -> Dict[str, Any]:
    """
    Lists the files in a directory provided by the user.
    :param path: The path to a directory to list files from.
    :return: A list of files in the directory.
    """
    full_path = resolve_abs_path(path)
    all_files = []
    for item in full_path.iterdir():
        all_files.append({
            "filename": item.name,
            "type": "file" if item.is_file() else "dir"
        })
    return {
        "path": str(full_path),
        "files": all_files
    }

def edit_file_tool(path: str, old_str: str, new_str: str) -> Dict[str, Any]:
    """
    Replaces first occurrence of old_str with new_str in file. If old_str is empty,
    create/overwrite file with new_str.
    :param path: The path to the file to edit.
    :param old_str: The string to replace.
    :param new_str: The string to replace with.
    :return: A dictionary with the path to the file and the action taken.
    """
    full_path = resolve_abs_path(path)
    if old_str == "":
        full_path.write_text(new_str, encoding="utf-8")
        return {
            "path": str(full_path),
            "action": "created_file"
        }
    original = full_path.read_text(encoding="utf-8")
    if original.find(old_str) == -1:
        return {
            "path": str(full_path),
            "action": "old_str not found"
        }
    edited = original.replace(old_str, new_str, 1)
    full_path.write_text(edited, encoding="utf-8")
    return {
        "path": str(full_path),
        "action": "edited"
    }


TOOL_REGISTRY = {
    "read_file": read_file_tool,
    "list_files": list_files_tool,
    "edit_file": edit_file_tool
}

def get_tool_str_representation(tool_name: str) -> str:
    tool = TOOL_REGISTRY[tool_name]
    return f"""
    Name: {tool_name}
    Description: {tool.__doc__}
    Signature: {inspect.signature(tool)}
    """

def get_full_system_prompt():
    tool_str_repr = ""
    for tool_name in TOOL_REGISTRY:
        tool_str_repr += "TOOL\n===" + get_tool_str_representation(tool_name)
        tool_str_repr += f"\n{'=' * 15}\n"
    return SYSTEM_PROMPT.format(tool_list_repr=tool_str_repr)

def extract_tool_invocations(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Return list of (tool_name, args) requested in 'tool: name({...})' lines.
    The parser expects single-line, compact JSON in parentheses.
    """
    invocations = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("tool:"):
            continue
        try:
            after = line[len("tool:"):].strip()
            name, rest = after.split("(", 1)
            name = name.strip()
            if not rest.endswith(")"):
                continue
            json_str = rest[:-1].strip()
            args = json.loads(json_str)
            invocations.append((name, args))
        except Exception:
            continue
    return invocations

def execute_llm_call(conversation: List[Dict[str, str]]):
    # O plano gratuito do Groq limita a saída a 1000 tokens/minuto (OTPM):
    # cada chamada precisa caber nisso, e ao estourar o limite esperamos e tentamos de novo.
    for attempt in range(5):
        try:
            response = openai_client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=conversation,
                max_completion_tokens=900
            )
            return response.choices[0].message.content
        except RateLimitError:
            print(f"[rate limit do Groq] aguardando 60s (tentativa {attempt + 1}/5)...")
            time.sleep(60)
    raise RuntimeError("Rate limit do Groq persistiu após 5 tentativas")

def run_coding_agent_loop():
    print(get_full_system_prompt())
    conversation = [{
        "role": "system",
        "content": get_full_system_prompt()
    }]
    while True:
        try:
            user_input = input(f"{YOU_COLOR}You:{RESET_COLOR}:")
        except (KeyboardInterrupt, EOFError):
            break
        conversation.append({
            "role": "user",
            "content": user_input.strip()
        })
        log_trace(f"\n{'#' * 60}\nUSER: {user_input.strip()}\n{'#' * 60}")
        iteration = 0
        while True:
            iteration += 1
            assistant_response = execute_llm_call(conversation)
            tool_invocations = extract_tool_invocations(assistant_response)
            log_trace(f"\n===== Iteração {iteration} =====")
            log_trace(f"Thought:\n{extract_thought(assistant_response) or '(vazio)'}")
            # A resposta do assistente entra na conversa mesmo quando tem tool calls,
            # para que o LLM veja o próprio raciocínio nas chamadas seguintes.
            conversation.append({
                "role": "assistant",
                "content": assistant_response
            })
            if not tool_invocations:
                print(f"{ASSISTANT_COLOR}Assistant:{RESET_COLOR}: {assistant_response}")
                log_trace("Action: nenhuma (resposta final)")
                break
            for name, args in tool_invocations:
                tool = TOOL_REGISTRY[name]
                resp = ""
                print(name, args)
                log_trace(f"Action: {name}({json.dumps(args, ensure_ascii=False)})")
                if name == "read_file":
                    resp = tool(args.get("filename", "."))
                elif name == "list_files":
                    resp = tool(args.get("path", "."))
                elif name == "edit_file":
                    resp = tool(args.get("path", "."),
                                args.get("old_str", ""),
                                args.get("new_str", ""))
                observation = f"tool_result({json.dumps(resp)})"
                log_trace(f"Observation:\n{observation}")
                conversation.append({
                    "role": "user",
                    "content": observation
                })


if __name__ == "__main__":
    run_coding_agent_loop()

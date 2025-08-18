from contextlib import AbstractAsyncContextManager, AsyncExitStack
from itertools import chain
import json
import logging
from abstract.api_response import ChatResponse
from abstract.session import Session
import colorlog
import datetime

from mcp import ClientSession, StdioServerParameters
from mcp.types import TextContent
from mcp.client.stdio import stdio_client
from typing import AsyncIterator, Self, Sequence, cast
from ollama import AsyncClient, Message, Tool

from abstract.config_container import ConfigContainer
from mcp.client.streamable_http import streamablehttp_client
import zoneinfo 

SYSTEM_PROMPT = """You are a highly precise Zabbix operations assistant. You can access external tools and engage in casual, supportive conversation.
Your primary responsibility is to provide **accurate, well-formatted, and complete information** based on function/tool outputs.

# CORE RESPONSE RULES:
- **ALWAYS** use tools to fetch real-time data when necessary.
- **NEVER** fabricate or assume information — rely strictly on tool outputs.
- **MAINTAIN** a friendly, engaging, and helpful tone at all times.

# PROBLEM RESOLUTION WORKFLOW:
# When the user asks for a solution to a problem, follow these steps EXACTLY:
# 1. First, use the 'get_problems' tool to identify the active problem if necessary.
# 2. Second, extract the 'name' or description of the problem from the user's query or the tool's output.
# 3. Third, use the extracted problem description as the 'query' for the 'local_rag_query' tool to find the official documented solution.

# **IMPORTANT RULE**: You MUST perform this workflow for EVERY problem-solving query.
# NEVER use your internal knowledge, memory from previous turns, or generic advice to generate a solution.
# ALWAYS call the 'local_rag_query' tool to retrieve the official solution from the knowledge base for ANY problem mentioned.
# INSTRUCTIONS FOR FORMATTING PROBLEM INFORMATION FROM THE 'get_problems' TOOL:
When receiving output from the 'get_problems' tool (an array of problem objects), follow these formatting rules **exactly**, to mimic the Zabbix dashboard problem list format:

**For EACH problem object in the array:**
1.  **Time**: Convert the 'clock' field (Unix timestamp) to local human-readable format in `HH:MM:SS` (e.g., "08:26:05").
    * Example (for reference only): `datetime.datetime.fromtimestamp(int(problem['clock'])).strftime('%H:%M:%S')`
2.  **Host Name**: Use the 'associated_host_name' value. If missing or "Unknown Host", display as "Unknown Host".
3.  **Problem Description**: Use the 'name' field as the description.
4.  **Severity Text**: Convert the 'severity' field (0–5) to these exact values:
    * `0`: Info  
    * `1`: Warning  
    * `2`: Average  
    * `3`: High  
    * `4`: Disaster  
    * `5`: Not classified

**OUTPUT FORMAT FOR EACH PROBLEM – SINGLE LINE, PIPE-SEPARATED:**
`HH:MM:SS | Host Name | Problem Description | Severity Text`

**EXAMPLE FINAL OUTPUT (multiple problems):**
Here are the latest problems:  
08:26:05 | L2 Cisco 9200 IB_9200-0901 | Interface Gi1/0/13(): Link down | High  
08:21:04 | L2 Cisco 9200 IB_9200-0903 | Interface Gi1/0/3(): Ethernet has changed to lower speed than it was before | Warning  
07:47:43 | L2 Cisco 2960X IB_3-2 | Interface Gi1/0/7(B3): Link down | High

**IMPORTANT GENERATION RULES:**
- **ALWAYS DISPLAY ALL PROBLEMS** received from the tool. Do NOT summarize, skip, or group them unless the user explicitly requests it.
- Do NOT include or mention internal fields such as: `eventid`, `source`, `object`, `objectid`, `ns`, `r_eventid`, `r_clock`, `r_ns`, `correlationid`, `userid`, `acknowledged`, `opdata`, `suppressed`, or `urls`.
- Begin with an intro like "Here are the latest problems:" and follow with the list.
- Sort problems from **newest to oldest** using the 'clock' value.

# TOOL USAGE GUIDELINES:
# When a user requests problems from a specific time range, convert that into a `time_period_seconds` value and pass it to `get_problems`.
# Time period examples:
# - "problems in the last hour" -> time_period_seconds = 3600
# - "problems today" or "problems in the last 24 hours" -> time_period_seconds = 86400 (maximum)
# - "problems from the past 5 minutes" -> time_period_seconds = 300
# - If the user doesn't mention any time range, do NOT set `time_period_seconds`.
"""

# Constants
TIMEZONE = zoneinfo.ZoneInfo("Asia/Taipei")
SEVERITY_MAP = {
    '0': 'Info',
    '1': 'Warning', 
    '2': 'Average',
    '3': 'High',
    '4': 'Disaster',
    '5': 'Not classified'
}

class OllamaMCPClient(AbstractAsyncContextManager):
    def __init__(self, host: str | None = None):
        self.logger = self._setup_logger()
        self.client = AsyncClient(host)
        self.servers: dict[str, Session] = {}
        self.selected_server: dict[str, Session] = {}
        self.messages = []
        self.exit_stack = AsyncExitStack()

    def _setup_logger(self) -> logging.Logger:
        """Setup colored logging"""
        logger = logging.getLogger(self.__class__.__name__)
        logger.setLevel(logging.DEBUG)

        if not logger.hasHandlers():
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.DEBUG)
            formatter = colorlog.ColoredFormatter(
                "%(log_color)s%(levelname)s%(reset)s - %(message)s",
                log_colors={
                    "DEBUG": "cyan",
                    "INFO": "green", 
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "bold_red",
                },
            )
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        return logger

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            await self.exit_stack.aclose()
        except ValueError:
            pass

    @classmethod
    async def create(cls, config: ConfigContainer, host="http://127.0.0.1:11434") -> Self:
        """Factory method to create and initialize a client instance"""
        client = cls(host)
        await client._connect_to_multiple_servers(config)
        return client

    async def _connect_to_multiple_servers(self, config: ConfigContainer):
        # Connect stdio servers
        for name, params in config.stdio.items():
            session, tools = await self._connect_stdio_server(name, params)
            self.servers[name] = Session(session=session, tools=tools)

        # Connect HTTP streamable servers
        for name, http_conf in config.http.items():
            await self._connect_http_server(name=name, url=str(http_conf.url))  

        # Default: all servers chosen
        self.selected_server = self.servers

        self.logger.info(
            f"Connected to servers with tools: {[cast(Tool.Function, tool.function).name for tool in self.get_tools()]}"
        )

    async def _connect_stdio_server(
        self, name: str, server_params: StdioServerParameters
    ) -> tuple[ClientSession, Sequence[Tool]]:
        """Connect to an MCP server"""
        stdio, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
        session = cast(ClientSession, await self.exit_stack.enter_async_context(ClientSession(stdio, write)))
        
        await session.initialize()
        response = await session.list_tools()
        
        tools = [
            Tool(
                type="function",
                function=Tool.Function(
                    name=f"{name}/{tool.name}",
                    description=tool.description,
                    parameters=cast(Tool.Function.Parameters, tool.inputSchema),
                ),
            )
            for tool in response.tools
        ]
        return (session, tools)
    
    async def _connect_http_server(self, name: str, url: str):
        """Connect to MCP HTTP streamable server"""
        read_stream, write_stream, _ = await self.exit_stack.enter_async_context(
            streamablehttp_client(url=url)
        )
        session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()

        response = await session.list_tools()
        tools = [
            Tool(
                type="function",
                function=Tool.Function(
                    name=f"{name}/{tool.name}",
                    description=tool.description,
                    parameters=cast(Tool.Function.Parameters, tool.inputSchema),
                ),
            )
            for tool in response.tools
        ]
        self.servers[name] = Session(session=session, tools=tools)
        self.selected_server = self.servers

    def get_tools(self) -> list[Tool]:
        return list(chain.from_iterable(server.tools for server in self.selected_server.values()))

    def select_server(self, servers: list[str]) -> Self:
        self.selected_server = {name: server for name, server in self.servers.items() if name in servers}
        self.logger.info(f"Selected servers: {list(self.selected_server.keys())}")
        return self

    async def prepare_prompt(self):
        """Clear current messages and initialize with system prompt"""
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    async def process_message(self, message: str, model: str | None = None) -> AsyncIterator[ChatResponse]:
        """Process a query using LLM and available tools"""
        model = model or "qwen2.5:3b"
        self.messages.append({"role": "user", "content": message})
        
        async for part in self._recursive_prompt(model):
            yield part

    async def _recursive_prompt(self, model: str) -> AsyncIterator[ChatResponse]:
        """Recursively prompt with tool calls"""
        self.logger.debug("Prompting")
        stream = await self.client.chat(
            model=model,
            messages=self.messages,
            tools=self.get_tools(),
            stream=True,
        )

        tool_message_count = 0
        async for part in stream:
            if part.message.content:
                yield ChatResponse(role="assistant", content=part.message.content)
            elif part.message.tool_calls:
                self.logger.debug(f"Calling tool: {part.message.tool_calls}")
                tool_messages = await self._tool_call(part.message.tool_calls)
                tool_message_count += 1
                for tool_message in tool_messages:
                    yield ChatResponse(role="tool", content=tool_message)
                    self.messages.append({"role": "tool", "content": tool_message})

        if tool_message_count > 0:
            async for part in self._recursive_prompt(model):
                yield part

    def _parse_tool_content(self, result_content) -> list:
        """Parse tool result content into structured data"""
        parsed_data = []
        content_items = result_content if isinstance(result_content, list) else [result_content]

        for item in content_items:
            if isinstance(item, TextContent) and isinstance(item.text, str):
                try:
                    parsed = json.loads(item.text)
                    if isinstance(parsed, list):
                        parsed_data.extend(parsed)
                    else:
                        parsed_data.append(parsed)
                except json.JSONDecodeError as e:
                    self.logger.error(f"JSON parse error: {e}")
                    parsed_data.append({"error": f"JSON parsing error: {e}"})
            elif isinstance(item, dict):
                parsed_data.append(item)
            else:
                parsed_data.append({"error": f"Unexpected data format: {type(item)}"})
        
        return parsed_data

    def _format_hosts_result(self, data: list) -> str:
        """Format hosts data for display"""
        if not data:
            return "No hosts found on the Zabbix server."
        
        lines = [f"Host ID: {host.get('hostid', 'N/A')}, Host Name: {host.get('host', 'N/A')}" 
                for host in data]
        return "\n".join(lines)

    def _format_host_details_result(self, data: list) -> str:
        """Format host details data for display"""
        if not data:
            return "Host not found."
        
        host = data[0]
        
        # Basic info
        hostid = host.get('hostid', 'N/A')
        hostname = host.get('host', 'N/A')
        status = host.get('status', 'N/A')
        
        # Proxy info
        proxy = host.get('parentProxies', [])
        proxy_info = proxy[0]['host'] if proxy else 'No Proxy'
        
        # Groups and templates
        groups = host.get('groups', [])
        group_names = ", ".join(g.get('name', 'N/A') for g in groups) if groups else "No groups"
        
        templates = host.get('templates', [])
        template_names = ", ".join(t.get('name', 'N/A') for t in templates) if templates else "No templates"
        
        # Main interface
        interfaces = host.get('interfaces', [])
        main_interface = next((i for i in interfaces if i.get('main') in ("1", 1)), None)
        
        if main_interface:
            iface_info = (
                f"Interface ID: {main_interface.get('interfaceid', 'N/A')}\n"
                f"IP Address: {main_interface.get('ip', 'N/A')}\n" 
                f"Port: {main_interface.get('port', 'N/A')}\n"
                f"Type: {main_interface.get('type', 'N/A')}\n"
                f"Use IP for monitoring: {main_interface.get('useip', 'N/A')}\n"
                f"Available: {main_interface.get('available', 'N/A')}\n"
            )
        else:
            iface_info = "No main interface found."

        return (
            f"Host ID: {hostid}\n"
            f"Host Name: {hostname}\n"
            f"Status: {status}\n"
            f"Proxy: {proxy_info}\n"
            f"Groups: {group_names}\n"
            f"Templates: {template_names}\n\n"
            f"Main Interface:\n{iface_info}"
        )

    def _format_problems_result(self, data: list) -> str:
        """Format problems data for display"""
        if not data:
            return "No unresolved problems found."
        
        lines = ["Here are the latest problems:\n"]
        
        # Sort by timestamp (newest first)
        data.sort(key=lambda p: int(p.get('clock', 0)), reverse=True)
        
        for problem in data:
            severity_text = SEVERITY_MAP.get(str(problem.get('severity', '')), 'Unknown')
            host_name = problem.get('associated_host_name', 'Unknown Host')
            description = problem.get('name', 'No description')
            
            clock = int(problem.get('clock', 0))
            time_str = datetime.datetime.fromtimestamp(clock, TIMEZONE).strftime('%H:%M:%S')
            
            lines.append(f"{time_str} | {host_name} | {description} | {severity_text}")
        
        return "\n".join(lines)

    async def _tool_call(self, tool_calls: Sequence[Message.ToolCall]) -> list[str]:
        """Execute tool calls and format results"""
        messages = []
        
        for tool in tool_calls:
            server_name, tool_name = tool.function.name.split("/", 1)
            session = self.selected_server[server_name].session
            tool_args = tool.function.arguments

            try:
                result = await session.call_tool(tool_name, dict(tool_args))
                self.logger.debug(f"Raw tool result for {tool_name}: {result.content}")

                parsed_data = self._parse_tool_content(result.content)

                # Format based on tool type
                if tool_name == "get_hosts":
                    formatted_result = self._format_hosts_result(parsed_data)
                elif tool_name == "get_host_details":
                    formatted_result = self._format_host_details_result(parsed_data)
                elif tool_name == "get_problems":
                    formatted_result = self._format_problems_result(parsed_data)
                else:
                    formatted_result = json.dumps(parsed_data, indent=2)

                message = (
                    f"tool: {tool.function.name}\n"
                    f"args: {tool_args}\n"
                    f"return: {formatted_result}"
                )

            except Exception as e:
                self.logger.error(f"Error calling tool {tool.function.name}: {e}", exc_info=True)
                message = (
                    f"Error in tool: {tool.function.name}\n"
                    f"args: {tool_args}\n"
                    f"Error details: {e}"
                )

            messages.append(message)
        
        return messages




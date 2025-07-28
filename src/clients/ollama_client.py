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


SYSTEM_PROMPT = """You are a highly precise Zabbix operations assistant. You can access external tools and engage in casual, supportive conversation.
Your primary responsibility is to provide **accurate, well-formatted, and complete information** based on function/tool outputs.

# CORE RESPONSE RULES:
- **ALWAYS** use tools to fetch real-time data when necessary.
- **NEVER** fabricate or assume information — rely strictly on tool outputs.
- **MAINTAIN** a friendly, engaging, and helpful tone at all times.

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
- Begin with an intro like “Here are the latest problems:” and follow with the list.
- Sort problems from **newest to oldest** using the 'clock' value.

# TOOL USAGE GUIDELINES:
# When a user requests problems from a specific time range, convert that into a `time_period_seconds` value and pass it to `get_problems`.
# Time period examples:
# - "problems in the last hour" -> time_period_seconds = 3600
# - "problems today" or "problems in the last 24 hours" -> time_period_seconds = 86400 (maximum)
# - "problems from the past 5 minutes" -> time_period_seconds = 300
# - If the user doesn't mention any time range, do NOT set `time_period_seconds`.
"""


class OllamaMCPClient(AbstractAsyncContextManager):
    def __init__(self, host: str | None = None):
        # Setup logging
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(levelname)s%(reset)s - %(message)s",
            datefmt=None,
            reset=True,
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )

        console_handler.setFormatter(formatter)
        if not self.logger.hasHandlers():
            self.logger.addHandler(console_handler)

        # Initialize client objects
        self.client = AsyncClient(host)
        self.servers: dict[str, Session] = {}
        self.selected_server: dict[str, Session] = {}
        self.messages = []
        self.exit_stack = AsyncExitStack()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            await self.exit_stack.aclose()
        except ValueError:
            return

    @classmethod
    async def create(cls, config: ConfigContainer, host="http://127.0.0.1:11434") -> Self:
        """Factory method to create and initialize a client instance"""
        client = cls(host)
        await client._connect_to_multiple_servers(config)
        return client

    async def _connect_to_multiple_servers(self, config: ConfigContainer):
        # Connect stdio servers
        for name, params in config.stdio.items():
            session, tools = await self._connect_to_server(name, params)
            self.servers[name] = Session(session=session, tools=tools)

        # Connect HTTP streamable servers
        for name, http_conf in config.http.items():
            await self.connect_http_server(name=name, url=str(http_conf.url))  

        # Default: all server that choosen
        self.selected_server = self.servers

        self.logger.info(
            f"Connected to server with tools: {[cast(Tool.Function, tool.function).name for tool in self.get_tools()]}"
        )


    async def _connect_to_server(
        self, name: str, server_params: StdioServerParameters
    ) -> tuple[ClientSession, Sequence[Tool]]:
        """Connect to an MCP server

        Args:
            server_script_path: Path to the server script (.py)
        """
        stdio, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
        session = cast(ClientSession, await self.exit_stack.enter_async_context(ClientSession(stdio, write)))

        await session.initialize()

        # List available tools
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
    
    #http server#
    async def connect_http_server(self, name: str, url: str):
        """Connect to MCP HTTP streamable server manually"""
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
        self.selected_server = self.servers  # optional: auto-select


    def get_tools(self) -> list[Tool]:
        return list(chain.from_iterable(server.tools for server in self.selected_server.values()))

    def select_server(self, servers: list[str]) -> Self:
        self.selected_server = {name: server for name, server in self.servers.items() if name in servers}
        self.logger.info(f"Selected server: {list(self.selected_server.keys())}")
        return self

    async def prepare_prompt(self):
        """Clear current message and create new one"""

        # Get all prompt with name "default"
        # all_prompts = [
        #     (server, prompt)
        #     for server in self.selected_server.values()
        #     for prompt in (await server.session.list_prompts()).prompts
        # ]
        # default_prompts = [
        #     cast(TextContent, (await server.session.get_prompt(prompt.name)).messages[0].content).text
        #     for server, prompt in all_prompts
        #     if prompt.name == "default"
        # ]
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # + [
        #     {"role": "system", "content": prompt} for prompt in default_prompts
        # ]

    async def process_message(self, message: str, model: str | None = None) -> AsyncIterator[ChatResponse]:
        """Process a query using LLM and available tools"""
        if model is None:
            model = "qwen2.5:3b"  # Predefined model
        self.messages.append({"role": "user", "content": message})

        async for part in self._recursive_prompt(model):
            yield part

    async def _recursive_prompt(self, model: str) -> AsyncIterator[ChatResponse]:
        # self.logger.debug(f"message: {self.messages}")
        # Streaming does not work when provided with tools, that's the issue with API or ollama itself.
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

    async def _tool_call(self, tool_calls: Sequence[Message.ToolCall]) -> list[str]:
        messages: list[str] = []
        # Define severity mapping within the code for consistency
        severity_map = {
            '0': 'Info',
            '1': 'Warning',
            '2': 'Average',
            '3': 'High',
            '4': 'Disaster',
            '5': 'Not classified'
        }
        
        for tool in tool_calls:
            # Safely split server name and tool name (e.g., "zabbix/get_hosts")
            server_name, tool_name = tool.function.name.split("/", 1)
            session = self.selected_server[server_name].session
            tool_args = tool.function.arguments

            try:
                # Execute the tool call
                result = await session.call_tool(tool_name, dict(tool_args))
                self.logger.debug(f"Raw tool call result content for {tool_name}: {result.content}")

                parsed_tool_data = []

                # Ensure result.content is processed as a list of items
                content_items = result.content if isinstance(result.content, list) else [result.content]

                for item_content in content_items:
                    if isinstance(item_content, TextContent) and isinstance(item_content.text, str):
                        try:
                            item_dict = json.loads(item_content.text)
                            
                            # --- COMMON PROCESSING FOR PROBLEMS ---
                            if tool_name == "get_problems":
                                # Convert 'clock' timestamp to HH:MM:SS
                                if 'clock' in item_dict and item_dict['clock'] is not None:
                                    try:
                                        timestamp = int(item_dict['clock'])
                                        item_dict['formatted_time'] = datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                                    except (ValueError, TypeError):
                                        item_dict['formatted_time'] = "N/A Time"
                                else:
                                    item_dict['formatted_time'] = "N/A Time"

                                # Convert 'severity' to text
                                if 'severity' in item_dict and item_dict['severity'] is not None:
                                    item_dict['severity_text'] = severity_map.get(str(item_dict['severity']), 'Unknown Severity')
                                else:
                                    item_dict['severity_text'] = 'Unknown Severity'
                            
                            # --- NO SPECIFIC PROCESSING FOR get_host_details HERE ---
                            # LLM will handle its interpretation directly from raw JSON
                            
                            parsed_tool_data.append(item_dict)
                        except json.JSONDecodeError as e:
                            self.logger.error(f"Error parsing JSON from TextContent: {item_content.text} - {e}")
                            parsed_tool_data.append({"error": f"JSON parsing error: {e}"}) # Return dict for consistency
                    elif isinstance(item_content, dict): # Assume it's already a parsed dict
                        # --- COMMON PROCESSING FOR PROBLEMS if already a dict ---
                        if tool_name == "get_problems":
                            if 'clock' in item_content and item_content['clock'] is not None:
                                try:
                                    timestamp = int(item_content['clock'])
                                    item_content['formatted_time'] = datetime.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                                except (ValueError, TypeError):
                                    item_content['formatted_time'] = "N/A Time"
                            else:
                                item_content['formatted_time'] = "N/A Time"

                            if 'severity' in item_content and item_content['severity'] is not None:
                                item_content['severity_text'] = severity_map.get(str(item_content['severity']), 'Unknown Severity')
                            else:
                                item_content['severity_text'] = 'Unknown Severity'
                        
                        # --- NO SPECIFIC PROCESSING FOR get_host_details HERE ---
                        # LLM will handle its interpretation directly from raw JSON
                        
                        parsed_tool_data.append(item_content)
                    else:
                        self.logger.warning(f"Unexpected item type in tool result: {type(item_content)}. Expected TextContent or dict.")
                        parsed_tool_data.append({"error": f"Unexpected data format: {type(item_content)}"}) # Return dict for consistency

                # Format the parsed data into a pretty-printed JSON string for the LLM
                # The LLM will now receive 'formatted_time' and 'severity_text' directly for problems.
                # For host details, it receives raw JSON.
                formatted_result_for_llm = json.dumps(parsed_tool_data, indent=2)
                self.logger.debug(f"Formatted tool result for LLM: {formatted_result_for_llm}")

                # Construct the message containing tool execution details and its formatted return
                message = (
                    f"tool: {tool.function.name}\n"
                    f"args: {tool_args}\n"
                    f"return: {formatted_result_for_llm}"
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


from __future__ import annotations

"""
Base class for backends using OpenAI Chat Completions API format.
Handles common message processing, tool conversion, and streaming patterns.
"""

import os
import asyncio
from typing import Dict, List, Any, AsyncGenerator, Optional
from .base import LLMBackend, StreamChunk


class ChatCompletionsBackend(LLMBackend):
    """Complete OpenAI-compatible Chat Completions API backend.
    
    Can be used directly with any OpenAI-compatible provider by setting base_url.
    Supports Cerebras AI and other compatible providers.
    """

    def __init__(self, api_key: Optional[str] = None, **kwargs):
        super().__init__(api_key, **kwargs)
        # Get API key from parameter, environment, or default to OpenAI
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        # Get base_url from backend_params or use OpenAI default
        self.base_url = kwargs.get("base_url", "https://api.openai.com/v1")
        # Store provider name for identification
        self._provider_name = kwargs.get("provider_name", "OpenAI")

    def convert_tools_to_chat_completions_format(
        self, tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert tools from Response API format to Chat Completions format if needed.

        Response API format: {"type": "function", "name": ..., "description": ..., "parameters": ...}
        Chat Completions format: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        if not tools:
            return tools

        converted_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                if "function" in tool:
                    # Already in Chat Completions format
                    converted_tools.append(tool)
                elif "name" in tool and "description" in tool:
                    # Response API format - convert to Chat Completions format
                    converted_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool.get("parameters", {}),
                            },
                        }
                    )
                else:
                    # Unknown format - keep as-is
                    converted_tools.append(tool)
            else:
                # Non-function tool - keep as-is
                converted_tools.append(tool)

        return converted_tools

    async def handle_chat_completions_stream(
        self, stream, enable_web_search: bool = False
    ) -> AsyncGenerator[StreamChunk, None]:
        """Handle standard Chat Completions API streaming format."""
        import json

        content = ""
        current_tool_calls = {}
        search_sources_used = 0
        citations = []

        async for chunk in stream:
            try:
                if hasattr(chunk, "choices") and chunk.choices:
                    choice = chunk.choices[0]

                    # Handle content delta
                    if hasattr(choice, "delta") and choice.delta:
                        delta = choice.delta

                        # Plain text content
                        if getattr(delta, "content", None):
                            content_chunk = delta.content
                            content += content_chunk
                            yield StreamChunk(type="content", content=content_chunk)
                        
                        # Tool calls streaming (OpenAI-style)
                        if getattr(delta, "tool_calls", None):
                            for tool_call_delta in delta.tool_calls:
                                index = getattr(tool_call_delta, "index", 0)

                                if index not in current_tool_calls:
                                    current_tool_calls[index] = {
                                        "id": "",
                                        "function": {
                                            "name": "",
                                            "arguments": "",
                                        },
                                    }

                                # Accumulate id
                                if getattr(tool_call_delta, "id", None):
                                    current_tool_calls[index]["id"] = tool_call_delta.id

                                # Function name
                                if (
                                    hasattr(tool_call_delta, "function")
                                    and tool_call_delta.function
                                ):
                                    if getattr(tool_call_delta.function, "name", None):
                                        current_tool_calls[index]["function"][
                                            "name"
                                        ] = tool_call_delta.function.name

                                    # Accumulate arguments (as string chunks)
                                    if getattr(tool_call_delta.function, "arguments", None):
                                        current_tool_calls[index]["function"][
                                            "arguments"
                                        ] += tool_call_delta.function.arguments

                    # Handle finish reason
                    if getattr(choice, "finish_reason", None):
                        if choice.finish_reason == "tool_calls" and current_tool_calls:

                            final_tool_calls = []

                            for index in sorted(current_tool_calls.keys()):
                                call = current_tool_calls[index]
                                function_name = call["function"]["name"]
                                arguments_str = call["function"]["arguments"]

                                try:
                                    arguments_obj = (
                                        json.loads(arguments_str)
                                        if arguments_str.strip()
                                        else {}
                                    )
                                except json.JSONDecodeError:
                                    arguments_obj = {}

                                final_tool_calls.append(
                                    {
                                        "id": call["id"] or f"toolcall_{index}",
                                        "type": "function",
                                        "function": {
                                            "name": function_name,
                                            "arguments": arguments_obj,
                                        },
                                    }
                                )

                            yield StreamChunk(
                                type="tool_calls", tool_calls=final_tool_calls
                            )

                            complete_message = {
                                "role": "assistant",
                                "content": content.strip(),
                                "tool_calls": final_tool_calls,
                            }

                            yield StreamChunk(
                                type="complete_message",
                                complete_message=complete_message,
                            )
                            yield StreamChunk(type="done")
                            return

                        elif choice.finish_reason in ["stop", "length"]:
                            if search_sources_used > 0:
                                yield StreamChunk(
                                    type="content",
                                    content=f"\n✅ [Live Search Complete] Used {search_sources_used} sources\n",
                                )

                            # Handle citations if present
                            if hasattr(chunk, "citations") and chunk.citations:
                                if enable_web_search:
                                    citation_text = "\n📚 **Citations:**\n"
                                    for i, citation in enumerate(chunk.citations, 1):
                                        citation_text += f"{i}. {citation}\n"
                                    yield StreamChunk(
                                        type="content", content=citation_text
                                    )

                            # Return final message
                            complete_message = {
                                "role": "assistant",
                                "content": content.strip(),
                            }
                            yield StreamChunk(
                                type="complete_message",
                                complete_message=complete_message,
                            )
                            yield StreamChunk(type="done")
                            return

                # Optionally handle usage metadata
                if hasattr(chunk, "usage") and chunk.usage:
                    if getattr(chunk.usage, "num_sources_used", 0) > 0:
                        search_sources_used = chunk.usage.num_sources_used
                        if enable_web_search:
                            yield StreamChunk(
                                type="content",
                                content=f"\n📊 [Live Search] Using {search_sources_used} sources for real-time data\n",
                            )

            except Exception as chunk_error:
                yield StreamChunk(
                    type="error", error=f"Chunk processing error: {chunk_error}"
                )
                continue

        # Fallback in case stream ends without finish_reason
        yield StreamChunk(type="done")


    async def stream_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], **kwargs
    ) -> AsyncGenerator[StreamChunk, None]:
            """Stream response using OpenAI-compatible Chat Completions API.

            Supports:
            - Cerebras Cloud (native SDK)
            - Any OpenAI-compatible provider via openai.AsyncOpenAI with base_url
            """
            try:
                base_url = kwargs.get("base_url") or self.base_url
                model = kwargs.get("model", "openai/gpt-oss-120b")
                max_tokens = kwargs.get("max_tokens", None)
                temperature = kwargs.get("temperature", None)
                enable_web_search = kwargs.get("enable_web_search", False)
                enable_code_interpreter = kwargs.get("enable_code_interpreter", False)

                # Convert tools to Chat Completions format
                converted_tools = (
                    self.convert_tools_to_chat_completions_format(tools) if tools else None
                )

                # Chat Completions API parameters
                api_params = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                }

                if converted_tools:
                    api_params["tools"] = converted_tools
                if max_tokens is not None:
                    api_params["max_tokens"] = max_tokens
                if temperature is not None:
                    api_params["temperature"] = temperature

                # Add provider tools (web search, code interpreter) if enabled
                provider_tools = []
                if enable_web_search:
                    provider_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "description": "Search the web for current or factual information",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "The search query to send to the web",
                                        }
                                    },
                                    "required": ["query"],
                                },
                            },
                        }
                    )

                if enable_code_interpreter:
                    provider_tools.append(
                        {"type": "code_interpreter", "container": {"type": "auto"}}
                    )

                if provider_tools:
                    if "tools" not in api_params:
                        api_params["tools"] = []
                    api_params["tools"].extend(provider_tools)

                # Decide client based on base_url
                if base_url and "cerebras.ai" in base_url:
                    from cerebras.cloud.sdk import AsyncCerebras

                    client = AsyncCerebras(api_key=self.api_key)
                    stream = await client.chat.completions.create(**api_params)
                else:
                    # Generic OpenAI-compatible path (OpenRouter, Dashscope compatible, etc.)
                    import openai

                    client = openai.AsyncOpenAI(api_key=self.api_key, base_url=base_url)
                    stream = await client.chat.completions.create(**api_params)

                async for chunk in self.handle_chat_completions_stream(
                    stream, enable_web_search
                ):
                    yield chunk

            except Exception as e:
                yield StreamChunk(type="error", error=f"Chat Completions API error: {str(e)}")

    def get_provider_name(self) -> str:
        """Get the name of this provider."""
        return self._provider_name

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (rough approximation)."""
        # Simple approximation: ~1.3 tokens per word
        return int(len(text.split()) * 1.3)

    def calculate_cost(
        self, input_tokens: int, output_tokens: int, model: str
    ) -> float:
        """Calculate cost for token usage based on OpenAI pricing (default fallback)."""
        model_lower = model.lower()
        
        # OpenAI GPT-4o pricing (most common)
        if "gpt-4o" in model_lower:
            if "mini" in model_lower:
                input_cost = (input_tokens / 1_000_000) * 0.15
                output_cost = (output_tokens / 1_000_000) * 0.60
            else:
                input_cost = (input_tokens / 1_000_000) * 2.50
                output_cost = (output_tokens / 1_000_000) * 10.00
        # GPT-4 pricing
        elif "gpt-4" in model_lower:
            if "turbo" in model_lower:
                input_cost = (input_tokens / 1_000_000) * 10.00
                output_cost = (output_tokens / 1_000_000) * 30.00
            else:
                input_cost = (input_tokens / 1_000_000) * 30.00
                output_cost = (output_tokens / 1_000_000) * 60.00
        # GPT-3.5 pricing
        elif "gpt-3.5" in model_lower:
            input_cost = (input_tokens / 1_000_000) * 0.50
            output_cost = (output_tokens / 1_000_000) * 1.50
        else:
            # Generic fallback pricing (moderate cost estimate)
            input_cost = (input_tokens / 1_000_000) * 1.00
            output_cost = (output_tokens / 1_000_000) * 3.00

        return input_cost + output_cost

    def extract_tool_name(self, tool_call: Dict[str, Any]) -> str:
        """Extract tool name from Chat Completions format."""
        return tool_call.get("function", {}).get("name", "unknown")

    def extract_tool_arguments(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        """Extract tool arguments from Chat Completions format."""
        arguments = tool_call.get("function", {}).get("arguments", {})
        if isinstance(arguments, str):
            try:
                import json
                return json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return {}
        return arguments

    def extract_tool_call_id(self, tool_call: Dict[str, Any]) -> str:
        """Extract tool call ID from Chat Completions format."""
        return tool_call.get("id", "")

    def create_tool_result_message(
        self, tool_call: Dict[str, Any], result_content: str
    ) -> Dict[str, Any]:
        """Create tool result message for Chat Completions format."""
        tool_call_id = self.extract_tool_call_id(tool_call)
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result_content,
        }

    def extract_tool_result_content(self, tool_result_message: Dict[str, Any]) -> str:
        """Extract content from Chat Completions tool result message."""
        return tool_result_message.get("content", "")

    def get_supported_builtin_tools(self) -> List[str]:
        """Get list of builtin tools supported by this provider."""
        # Chat Completions API doesn't typically support builtin tools like web_search
        # But some providers might - this can be overridden in subclasses
        return []

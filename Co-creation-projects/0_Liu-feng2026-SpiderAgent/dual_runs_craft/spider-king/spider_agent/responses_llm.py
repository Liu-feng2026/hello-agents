"""Bridge HelloAgents tool calls to the Responses API.

Both comparison variants use this identical adapter. The provider configured for
this run accepts /responses but rejects /chat/completions for this model.
"""
from types import SimpleNamespace

from hello_agents import HelloAgentsLLM


class ResponsesLLM(HelloAgentsLLM):
    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        adapter = self._adapter
        if adapter._client is None:
            adapter._client = adapter.create_client()

        items = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": str(message["content"]),
                })
                continue

            content = message.get("content")
            if content:
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls", []):
                items.append({
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                })

        functions = []
        for tool in tools:
            function = tool["function"]
            flattened = {
                "type": "function",
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function["parameters"],
            }
            if "strict" in function:
                flattened["strict"] = function["strict"]
            functions.append(flattened)

        if isinstance(tool_choice, dict):
            tool_choice = {
                "type": "function",
                "name": tool_choice["function"]["name"],
            }

        request = {
            "model": self.model,
            "input": items,
            "tools": functions,
            "tool_choice": tool_choice,
        }
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        if max_tokens is not None:
            request["max_output_tokens"] = max_tokens
        response = adapter._client.responses.create(**request)
        if response.status != "completed":
            raise RuntimeError(
                f"Responses API returned {response.status}: "
                f"{response.incomplete_details}"
            )

        calls = [
            SimpleNamespace(
                id=item.call_id,
                function=SimpleNamespace(name=item.name, arguments=item.arguments),
            )
            for item in response.output
            if item.type == "function_call"
        ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=response.output_text or None,
                tool_calls=calls,
            ))],
            usage=SimpleNamespace(total_tokens=response.usage.total_tokens)
            if response.usage else None,
        )

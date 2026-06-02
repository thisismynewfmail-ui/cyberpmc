import json, time
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": "Omnibrain-UE", "object": "model"},
        {"id": "test-mini", "object": "model"},
    ]})

def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/v1/chat/completions")
def chat():
    body = request.get_json(force=True)
    # echo a few sampler keys back into the reply so the test can confirm pass-through
    seen = {k: body.get(k) for k in ("temperature", "dynamic_temperature", "repetition_penalty_range", "min_p")}
    msgs = body.get("messages", [])
    nmsg = len(msgs)
    has_tool_msg = any(m.get("role") == "tool" for m in msgs)
    tools = body.get("tools")
    # count image_url parts across the prompt so the test can confirm vision relay
    nimg = sum(
        1
        for m in msgs
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    )

    # --- Tool-loop branches (only when the client advertised `tools`) ---
    if has_tool_msg:
        # The model has already seen a tool result — produce the final answer,
        # quoting the observation so the test can confirm the hand-off worked.
        last_tool = [m for m in msgs if m.get("role") == "tool"][-1]
        observed = (last_tool.get("content") or "")[:80]

        def gen_final():
            for c in ["<think>", "got the tool output", "</think>",
                      "TOOLDONE ", f"result was: {observed} ", "// finished."]:
                yield _sse({"choices": [{"delta": {"content": c}}]})
                time.sleep(0.01)
            yield _sse({"choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58}})
            yield "data: [DONE]\n\n"
        return Response(gen_final(), mimetype="text/event-stream")

    if tools:
        # First hop: think, then request a call to the `echo` tool (arguments
        # streamed in fragments, exactly like a real backend).
        def gen_call():
            yield _sse({"choices": [{"delta": {"content": "<think>"}}]})
            yield _sse({"choices": [{"delta": {"content": "need to echo</think>"}}]})
            yield _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "echo", "arguments": ""}}]}}]})
            yield _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "{\"text\": \"ping\"}"}}]}}]})
            yield _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 6, "total_tokens": 46}})
            yield "data: [DONE]\n\n"
        return Response(gen_call(), mimetype="text/event-stream")

    def gen():
        chunks = ["<think>", f"plan with {nmsg} msgs ", "and temp", "</think>",
                  "ACK ", f"images={nimg} ", f"samplers={json.dumps(seen)} ", "// done."]
        for c in chunks:
            yield _sse({'choices': [{'delta': {'content': c}}]})
            time.sleep(0.01)
        yield _sse({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 123, 'completion_tokens': 14, 'total_tokens': 137}})
        yield "data: [DONE]\n\n"
    return Response(gen(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5099, threaded=True)

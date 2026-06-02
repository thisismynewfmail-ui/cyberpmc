import json, time
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": "Omnibrain-UE", "object": "model"},
        {"id": "test-mini", "object": "model"},
    ]})

@app.post("/v1/chat/completions")
def chat():
    body = request.get_json(force=True)
    # echo a few sampler keys back into the reply so the test can confirm pass-through
    seen = {k: body.get(k) for k in ("temperature", "dynamic_temperature", "repetition_penalty_range", "min_p")}
    msgs = body.get("messages", [])
    nmsg = len(msgs)
    # count image_url parts across the prompt so the test can confirm vision relay
    nimg = sum(
        1
        for m in msgs
        if isinstance(m.get("content"), list)
        for part in m["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    )
    def gen():
        chunks = ["<think>", f"plan with {nmsg} msgs ", "and temp", "</think>",
                  "ACK ", f"images={nimg} ", f"samplers={json.dumps(seen)} ", "// done."]
        for c in chunks:
            yield f"data: {json.dumps({'choices':[{'delta':{'content':c}}]})}\n\n"
            time.sleep(0.01)
        yield f"data: {json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':123,'completion_tokens':14,'total_tokens':137}})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(gen(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5099, threaded=True)

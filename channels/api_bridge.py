import core
import asyncio
import time
import uuid
import uvicorn
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

class ApiBridge(core.channel.Channel):
    """
    An OpenAI-compatible API bridge for OpenLumara.
    This channel allows external applications to interact with OpenLumara 
    using the standard OpenAI Chat Completions, Models, and Embeddings APIs.
    """

    # -------------------------
    #   CONFIGURATION
    # -------------------------

    settings = {
        "host": {
            "type": "string",
            "description": "The host address for the API server.",
            "default": "0.0.0.0"
        },
        "port": {
            "type": "number",
            "description": "The port for the API server.",
            "default": 8000
        },
        "api_key_required": {
            "type": "boolean",
            "description": "If enabled, requires an 'Authorization: Bearer <key>' header. The key is set in 'api_key'.",
            "default": False
        },
        "api_key": {
            "type": "string",
            "description": "The API key required if 'api_key_required' is True.",
            "default": "sk-openlumara-dummy-key"
        }
    }

    dependencies = ["httpx", "fastapi", "uvicorn", "pydantic"]

    # -------------------------
    #   MODELS (OpenAI Spec)
    # -------------------------

    class ChatMessage(BaseModel):
        role: str
        content: Optional[str] = None
        name: Optional[str] = None

    class ChatCompletionRequest(BaseModel):
        model: str
        messages: List[ChatMessage]
        stream: Optional[bool] = False
        temperature: Optional[float] = 1.0
        top_p: Optional[float] = 1.0
        n: Optional[int] = 1
        max_tokens: Optional[int] = None
        stop: Optional[Union[str, List[str]]] = None
        presence_penalty: Optional[float] = 0.0
        frequency_penalty: Optional[float] = 0.0

    class Model(BaseModel):
        id: str
        object: str = "model"

    class ModelsResponse(BaseModel):
        object: str = "list"
        data: List[Model]

    class EmbeddingRequest(BaseModel):
        input: Union[str, List[str]]
        model: str
        encoding_format: Optional[str] = "float"

    class EmbeddingData(BaseModel):
        object: str = "embedding"
        embedding: List[float]
        index: int

    class EmbeddingResponse(BaseModel):
        object: str = "list"
        data: List[EmbeddingData]
        model: str
        usage: Dict[str, int]

    # -------------------------
    #   EVENT HANDLERS
    # -------------------------

    async def on_ready(self):
        self.log("api bridge", f"OpenAI Bridge Channel is ready! Listening on {self.config.get('host')}:{self.config.get('port')}")

    async def run(self):
        """The main loop: Starts the FastAPI server."""
        app = FastAPI(title="OpenLumara OpenAI Bridge")

        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if self.config.get("api_key_required"):
                auth_header = request.headers.get("Authorization")
                if not auth_header or auth_header != f"Bearer {self.config.get('api_key')}":
                    return JSONResponse(
                        status_code=401,
                        content={"error": {"message": "Invalid API key", "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}
                    )
            return await call_next(request)

        @app.get("/v1/models")
        async def list_models():
            """Returns a list of available models."""
            models = []
            print(await self.manager.API.list_models())
            for model_id in await self.manager.API.list_models():
                models.append(self.Model(id=model_id))
            return self.ModelsResponse(data=models)

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            body = await request.json()
            chat_req = self.ChatCompletionRequest(**body)

            if not chat_req.messages:
                raise HTTPException(status_code=400, detail="No messages provided")
            
            last_msg = chat_req.messages[-1]
            ol_message = {"role": last_msg.role, "content": last_msg.content}

            if chat_req.stream:
                return StreamingResponse(
                    self._stream_handler(ol_message, chat_req.model),
                    media_type="text/event-stream"
                )
            else:
                return await self._completion_handler(ol_message, chat_req.model)

        # Start the server
        config = uvicorn.Config(app, host=self.config.get("host"), port=self.config.get("port"), log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    async def _completion_handler(self, ol_message: dict, model: str) -> JSONResponse:
        try:
            # Send to OpenLumara
            response_dict = await self.send(ol_message, commands_authorized=True)
            # Format it
            response_dict = self.format_message(response_dict)
            content = response_dict.get("content", "")

            # Translate back to OpenAI
            return JSONResponse({
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0, 
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            })
        except Exception as e:
            self.log(self.name, f"Error in completion: {str(e)}")
            return JSONResponse(
                status_code=500,
                content={"error": {"message": str(e), "type": "server_error", "param": None, "code": "internal_error"}}
            )

    async def _stream_handler(self, ol_message: dict, model: str):
        try:
            chat_id = f"chatcmpl-{uuid.uuid4()}"
            created_time = int(time.time())

            # Initial empty chunk to satisfy some clients
            yield f"data: {self._openai_chunk(chat_id, created_time, model, '')}\n\n"

            async for token in self.format_stream_for_text(
                self.send_stream(ol_message, commands_authorized=True)
            ):
                token_type = token.get("type")
                token_content = token.get("content")

                if token_type == "content":
                    yield f"data: {self._openai_chunk(chat_id, created_time, model, token_content)}\n\n"

            print("data: [DONE]\n\n")
            yield "data: [DONE]\n\n"

        except Exception as e:
            self.log(self.name, f"Error in stream: {str(e)}")
            yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"

    def _openai_chunk(self, chat_id: str, created: int, model: str, delta: str) -> str:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": delta},
                "finish_reason": None
            }]
        }
        return json.dumps(chunk)

    async def on_push(self, msg):
        pass

    def on_log(self, cat, msg):
        print(f"[{cat}] {msg}")

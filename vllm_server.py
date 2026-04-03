#!/usr/bin/env python3
"""
vLLM inference server for Azure ML deployment.
Serves Qwen, Phi, Deepseek models with OpenAI-compatible API.

Usage:
  python vllm_server.py --model-name qwen2.5-72b
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

# vLLM imports
from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment configuration
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5-7b")
MODEL_PATH = os.environ.get("MODEL_PATH", f"/models/{MODEL_NAME}")
TENSOR_PARALLEL_SIZE = int(os.environ.get("TENSOR_PARALLEL_SIZE", "1"))
PIPELINE_PARALLEL_SIZE = int(os.environ.get("PIPELINE_PARALLEL_SIZE", "1"))
MAX_MODEL_LEN = int(os.environ.get("MAX_MODEL_LEN", "32768"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "64"))
DTYPE = os.environ.get("DTYPE", "bfloat16")

# Initialize FastAPI app
app = FastAPI(
    title="Claude Code Proxy - vLLM Server",
    description="OpenAI-compatible LLM API for open-source models",
    version="1.0.0"
)

# vLLM model instance (will be initialized at startup)
llm: Optional[LLM] = None

class ChatMessage(BaseModel):
    role: str = Field(..., description="Role: system, user, assistant")
    content: str = Field(..., description="Message content")

class TextCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    prompt: str = Field(..., description="Text prompt")
    max_tokens: int = Field(1024, ge=1, le=MAX_MODEL_LEN)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(0.9, ge=0.0, le=1.0)
    top_k: int = Field(40, ge=0, le=100)
    stop: Optional[List[str]] = None
    frequency_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    presence_penalty: float = Field(0.0, ge=-2.0, le=2.0)

class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: List[ChatMessage] = Field(..., description="Chat messages")
    max_tokens: int = Field(1024, ge=1, le=MAX_MODEL_LEN)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(0.9, ge=0.0, le=1.0)
    top_k: int = Field(40, ge=0, le=100)
    stop: Optional[List[str]] = None
    tools: Optional[List[Dict[str, Any]]] = None

class CompletionChoice(BaseModel):
    text: str
    finish_reason: str
    index: int

class TextCompletionResponse(BaseModel):
    model: str
    choices: List[CompletionChoice]
    usage: Dict[str, int]

class ChatChoice(BaseModel):
    message: ChatMessage
    finish_reason: str
    index: int

class ChatCompletionResponse(BaseModel):
    model: str
    choices: List[ChatChoice]
    usage: Dict[str, int]

class HealthResponse(BaseModel):
    status: str
    model_name: str
    tensor_parallel_size: int
    max_model_len: int

@app.on_event("startup")
async def startup():
    """Initialize vLLM model on startup."""
    global llm
    try:
        logger.info(f"Loading model: {MODEL_NAME}")
        logger.info(f"  Model path: {MODEL_PATH}")
        logger.info(f"  Tensor parallel: {TENSOR_PARALLEL_SIZE}")
        logger.info(f"  Max model len: {MAX_MODEL_LEN}")
        logger.info(f"  Dtype: {DTYPE}")

        llm = LLM(
            model=MODEL_PATH or MODEL_NAME,
            tensor_parallel_size=TENSOR_PARALLEL_SIZE,
            pipeline_parallel_size=PIPELINE_PARALLEL_SIZE,
            max_model_len=MAX_MODEL_LEN,
            dtype=DTYPE,
            gpu_memory_utilization=0.95,
            max_num_seqs=MAX_BATCH_SIZE,
            enforce_eager=False,  # Use PagedAttention for efficiency
        )
        logger.info(f"✓ Model loaded successfully: {MODEL_NAME}")
    except Exception as e:
        logger.error(f"✗ Failed to load model: {e}", exc_info=True)
        raise

@app.get("/health")
async def health() -> HealthResponse:
    """Health check endpoint."""
    if llm is None:
        raise HTTPException(status_code=503, detail="Model not initialized")

    return HealthResponse(
        status="ready",
        model_name=MODEL_NAME,
        tensor_parallel_size=TENSOR_PARALLEL_SIZE,
        max_model_len=MAX_MODEL_LEN
    )

@app.post("/v1/completions")
async def text_completion(request: TextCompletionRequest) -> TextCompletionResponse:
    """Text completion endpoint (OpenAI-compatible)."""
    if llm is None:
        raise HTTPException(status_code=503, detail="Model not initialized")

    try:
        sampling_params = SamplingParams(
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            stop=request.stop,
            frequency_penalty=request.frequency_penalty,
            presence_penalty=request.presence_penalty,
        )

        # Generate using vLLM
        outputs = llm.generate(request.prompt, sampling_params)
        output = outputs[0]

        return TextCompletionResponse(
            model=request.model,
            choices=[
                CompletionChoice(
                    text=output.outputs[0].text,
                    finish_reason=output.outputs[0].finish_reason,
                    index=0
                )
            ],
            usage={
                "prompt_tokens": len(output.prompt_token_ids),
                "completion_tokens": len(output.outputs[0].token_ids),
                "total_tokens": len(output.prompt_token_ids) + len(output.outputs[0].token_ids),
            }
        )
    except Exception as e:
        logger.error(f"Completion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/chat/completions")
async def chat_completion(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """Chat completion endpoint (OpenAI-compatible)."""
    if llm is None:
        raise HTTPException(status_code=503, detail="Model not initialized")

    try:
        # Convert chat messages to prompt string
        prompt = self._convert_messages_to_prompt(request.messages)

        sampling_params = SamplingParams(
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            stop=request.stop,
        )

        # Generate using vLLM
        outputs = llm.generate(prompt, sampling_params)
        output = outputs[0]

        return ChatCompletionResponse(
            model=request.model,
            choices=[
                ChatChoice(
                    message=ChatMessage(
                        role="assistant",
                        content=output.outputs[0].text
                    ),
                    finish_reason=output.outputs[0].finish_reason,
                    index=0
                )
            ],
            usage={
                "prompt_tokens": len(output.prompt_token_ids),
                "completion_tokens": len(output.outputs[0].token_ids),
                "total_tokens": len(output.prompt_token_ids) + len(output.outputs[0].token_ids),
            }
        )
    except Exception as e:
        logger.error(f"Chat completion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/models")
async def list_models() -> Dict[str, Any]:
    """List available models."""
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "owned_by": "vllm",
                "permission": [{"allow": "all"}],
            }
        ]
    }

def _convert_messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Convert chat messages to prompt string."""
    # This is a generic conversion; adjust based on model-specific formatting
    # Qwen, Phi use different formats
    prompt = ""
    for msg in messages:
        if msg.role == "system":
            prompt += f"System: {msg.content}\n"
        elif msg.role == "user":
            prompt += f"User: {msg.content}\n"
        elif msg.role == "assistant":
            prompt += f"Assistant: {msg.content}\n"
    prompt += "Assistant:"
    return prompt

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )

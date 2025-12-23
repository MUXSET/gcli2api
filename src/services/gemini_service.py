import json
import asyncio
from typing import Dict, Any, Optional, List

from config import (
    get_available_models,
    get_base_model_from_feature_model,
    get_base_model_name,
    is_anti_truncation_model,
    is_fake_streaming_model,
    get_anti_truncation_max_attempts,
)
from src.google_chat_api import build_gemini_payload_from_native, send_gemini_request
from src.openai_transfer import _extract_content_and_reasoning
from src.anti_truncation import apply_anti_truncation_to_stream
from log import log

class GeminiService:
    _instance = None
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = GeminiService()
        return cls._instance

    async def list_models(self) -> List[Dict[str, Any]]:
        """列出所有支持的模型"""
        models = []
        for model_id in get_available_models():
            base_model = get_base_model_name(model_id)
            models.append(
                {
                    "name": f"models/{model_id}",
                    "version": "001",
                    "displayName": f"{model_id} (GCLI Proxy)",
                    "description": f"Proxy model for {base_model}",
                    "inputTokenLimit": 30720,  # 示例值
                    "outputTokenLimit": 2048,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                    "temperature": 0.9,
                    "topP": 0.95,
                    "topK": 40,
                }
            )
        return models

    async def generate_content(
        self, 
        model_id: str, 
        payload: Dict[str, Any], 
        credentials: Dict[str, Any],
        is_stream: bool
    ) -> Any:
        try:
            # 1. 解析模型名称
            real_model = get_base_model_name(model_id)
            
            # 2. 检查特性
            use_anti_truncation = is_anti_truncation_model(model_id)
            use_fake_streaming = is_fake_streaming_model(model_id)

            log.info(f"Using model: {real_model} (Anti-Truncation: {use_anti_truncation}, Fake Streaming: {use_fake_streaming})")
            
            # 3. 构建Gemini Payload (如果已经是Native格式，这一步主要是透传或微调)
            # 这里我们假设Payload已经是兼容的格式，或者是需要微调的
            # src/routers/gemini.py 中原生调用其实是透传为主，但 build_gemini_payload_from_native 会做清理
            
            gemini_payload = build_gemini_payload_from_native(payload, real_model)
            
            # 4. 发送请求
            # 如果是 Fake Streaming，强制 stream=False 发送，然后转换
            request_stream_flag = is_stream
            if use_fake_streaming:
                request_stream_flag = False
            
            response = await send_gemini_request(
                real_model, 
                gemini_payload, 
                credentials, 
                stream=request_stream_flag
            )

            # 5. 处理响应
            if is_stream:
                if use_fake_streaming:
                    # 假流式：获取全量响应，切分转换
                    response_data = await response.json()
                    return self._handle_fake_streaming(response_data)
                elif use_anti_truncation:
                    # 防截断流式
                    return apply_anti_truncation_to_stream(
                        response.aiter_lines(), 
                        real_model, 
                        gemini_payload, 
                        credentials, 
                        get_anti_truncation_max_attempts()
                    )
                else:
                    # 普通流式
                    return self._handle_normal_streaming(response)
            else:
                # 非流式直接返回 JSON
                return await response.json()

        except Exception as e:
            log.error(f"GeminiService Error: {e}")
            raise

    async def _handle_normal_streaming(self, response):
        async for line in response.aiter_lines():
            if line:
                yield line + "\n"

    async def _handle_fake_streaming(self, response_data):
        """将非流式响应转换为流式块"""
        from src.google_chat_api import (
            extract_text_from_gemini_response,
            create_gemini_stream_chunk
        )
        
        full_text = extract_text_from_gemini_response(response_data)
        
        # 简单切分
        chunk_size = 10
        for i in range(0, len(full_text), chunk_size):
            chunk_text = full_text[i : i + chunk_size]
            chunk = create_gemini_stream_chunk(chunk_text)
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)
            
        # End
        yield "data: [DONE]\n\n"

gemini_service = GeminiService.get_instance()

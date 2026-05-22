from typing import Optional, List, Dict, Any
import aiohttp
import asyncio
import os
import re
import tempfile
from pydantic import Field
from pathlib import Path
from datetime import datetime
import urllib.parse
from app.schema import AgentState
from app.tool.base import BaseTool, ToolResult, CreditsManager
from app.logger import logger
from app.config import config, credits_config_manager
from app.utils.download_utils import DownloadUtils
from app.utils.polling import AsyncTaskPoller, TaskStatus
from app.utils.internal_auth import generate_token_direct

_VOICE_CLONING_DESCRIPTION = """
本工具用于语音克隆和语音合成。
它可以上传参考音频文件来克隆特定的声音，然后使用克隆的声音来合成新的语音内容。
支持多种语音合成模式：基础TTS、现有声音合成、自定义声音克隆等。
生成高质量的语音文件，适用于个性化配音、语音助手、音频内容创作等用途。

🎤 功能特点：

📤 语音克隆功能：
  • 上传参考音频文件（.wav格式）
  • 自动提取声音特征
  • 生成唯一的声音标识符
  • 支持多种音频质量和长度

🎯 语音合成模式：
  • 基础TTS：使用默认英文声音
  • 现有声音：使用预设的demo声音
  • 克隆声音：使用上传的自定义声音
  • 可调节语速（0.5-2.0倍）

🔊 支持的音频格式：
  • 输入：WAV格式音频文件
  • 输出：WAV格式高质量音频
  • 支持多种采样率和编码

💡 使用建议：
- 参考音频建议5-30秒，清晰无噪音
- 选择语调自然、发音标准的参考音频
- 合成文本建议与参考音频语言一致
- 支持中文和英文内容合成

🎭 应用场景：
- 个性化语音助手开发
- 音频内容创作和配音
- 语音广告和宣传制作
- 多语言语音合成
- 语音风格转换

---

This tool is used for voice cloning and speech synthesis.
It can upload reference audio files to clone specific voices, then use the cloned voices to synthesize new speech content.
Supports multiple speech synthesis modes: base TTS, existing voice synthesis, custom voice cloning, etc.
Generates high-quality speech files suitable for personalized dubbing, voice assistants, audio content creation, etc.

🎤 Features:

📤 Voice Cloning:
  • Upload reference audio files (.wav format)
  • Automatically extract voice characteristics
  • Generate unique voice identifiers
  • Support various audio qualities and lengths

🎯 Speech Synthesis Modes:
  • Base TTS: Use default English voice
  • Existing Voice: Use preset demo voices
  • Cloned Voice: Use uploaded custom voices
  • Adjustable speech speed (0.5-2.0x)

🔊 Supported Audio Formats:
  • Input: WAV format audio files
  • Output: WAV format high-quality audio
  • Support various sample rates and encodings

💡 Usage Tips:
- Reference audio should be 5-30 seconds, clear and noise-free
- Choose natural intonation and standard pronunciation reference audio
- Synthesis text should match the reference audio language
- Support Chinese and English content synthesis

🎭 Application Scenarios:
- Personalized voice assistant development
- Audio content creation and dubbing
- Voice advertising and promotional production
- Multilingual speech synthesis
- Voice style conversion
"""

# Valid speed range
VALID_SPEED_RANGE = (0.5, 2.0)

# Default demo voices available
DEFAULT_DEMO_VOICES = [
    "demo_speaker0",
    "demo_speaker1", 
    "demo_speaker2"
]

# Supported audio formats
SUPPORTED_AUDIO_FORMATS = [".wav", ".mp3", ".flac"]

class VoiceCloning(BaseTool):
    name: str = "voice_cloning"
    description: str = _VOICE_CLONING_DESCRIPTION
    parameters: dict = {
        "type": "object", 
        "properties": {
            "text": {
                "type": "string",
                "description": "需要转换为语音的文本内容。支持中文、英文等多种语言。 | Text content to be converted to speech. Supports multiple languages including Chinese and English.",
            },
            "mode": {
                "type": "string",
                "description": "语音合成模式：'base_tts'（基础TTS）、'existing_voice'（现有声音）、'clone_voice'（克隆声音）、'list_voices'（列出可用声音）。 | Speech synthesis mode: 'base_tts' (base TTS), 'existing_voice' (existing voice), 'clone_voice' (clone voice), 'list_voices' (list available voices).",
                "enum": ["base_tts", "existing_voice", "clone_voice", "list_voices"],
                "default": "base_tts"
            },
            "voice_name": {
                "type": "string",
                "description": "声音名称。对于existing_voice模式，使用demo_speaker0等；对于clone_voice模式，使用上传后返回的声音名称。 | Voice name. For existing_voice mode, use demo_speaker0 etc.; for clone_voice mode, use the voice name returned after upload.",
            },
            "reference_audio_path": {
                "type": "string",
                "description": "参考音频文件路径（仅clone_voice模式需要）。必须是WAV格式。 | Reference audio file path (only needed for clone_voice mode). Must be WAV format.",
            },
            "voice_label": {
                "type": "string",
                "description": "自定义声音标签名称（仅clone_voice模式需要）。用于标识克隆的声音。 | Custom voice label name (only needed for clone_voice mode). Used to identify the cloned voice.",
            },
            "model": {
                "type": "string",
                "description": "AI模型选择：F5TTS_v1_Base（默认）、F5TTS_Base、E2TTS_Base。 | AI model choice: F5TTS_v1_Base (default), F5TTS_Base, E2TTS_Base.",
                "enum": ["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base"],
                "default": "F5TTS_v1_Base"
            },
            "speed": {
                "type": "number",
                "description": "语速设置，范围0.5-2.0，1.0为正常语速。 | Speech speed setting, range 0.5-2.0, 1.0 is normal speed.",
                "default": 1.0,
                "minimum": 0.5,
                "maximum": 2.0
            },
            "remove_silence": {
                "type": "boolean",
                "description": "是否移除输出音频中的静音部分。 | Whether to remove silences from output audio.",
                "default": False
            },
            "seed": {
                "type": "integer",
                "description": "随机种子，-1为随机，其他值可重现结果。 | Random seed, -1 for random, other values for reproducible results.",
                "default": -1
            },
            "nfe_step": {
                "type": "integer",
                "description": "模型计算步数，影响质量和速度。 | Number of function evaluations, affects quality and speed.",
                "default": 32,
                "minimum": 16,
                "maximum": 64
            }
        },
        "required": ["text", "mode"],
    }

    # Define Pydantic fields for class attributes
    api_base_url: str = Field(default="", description="API base URL for voice cloning service")
    default_timeout: int = Field(default=300, description="Default API timeout in seconds")
    upload_timeout: int = Field(default=120, description="Upload timeout in seconds")
    api_headers: Dict[str, str] = Field(default_factory=dict, description="API request headers")
    async_poller: Optional[AsyncTaskPoller] = Field(default=None, description="Async task poller instance")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Get API configuration from config
        voice_cloning_config = getattr(config, 'voice_cloning_config', None)
        if voice_cloning_config:
            self.api_base_url = voice_cloning_config.api_base_url
            self.default_timeout = getattr(voice_cloning_config, 'default_timeout', 300)
            self.upload_timeout = getattr(voice_cloning_config, 'upload_timeout', 120)
            api_key = getattr(voice_cloning_config, 'api_key', None)
        else:
            # Fallback to environment variables or hardcoded values
            logger.warning("Voice cloning API configuration not found, using fallback values")
            self.api_base_url = os.environ.get("VOICE_CLONING_API_URL", "http://192.168.1.37:8010")
            self.default_timeout = 300
            self.upload_timeout = 120
            api_key = os.environ.get("VOICE_CLONING_API_KEY", None)

        # Generate token for user if available
        if self.user_id:
            api_key = generate_token_direct(self.user_id)
            logger.info(f"Generated token for user {self.user_id}")

        # Set up API headers
        self.api_headers = {
            "Accept": "application/json",
            "User-Agent": "e-ManusWeb/1.0"
        }
        
        # Add authentication if API key is provided
        if api_key:
            self.api_headers["Authorization"] = f"Bearer {api_key}"

        # Set up API endpoints for async poller
        api_endpoints = {
            "base_url": self.api_base_url,
            "status": f"{self.api_base_url}/voice-clone/status",
            "result": f"{self.api_base_url}/voice-clone/result"
        }
        
        self.async_poller = AsyncTaskPoller(api_endpoints, self.api_headers)
        
        logger.info(f"Voice cloning tool initialized with base URL: {self.api_base_url}")
        
    class Config:
        arbitrary_types_allowed = True

    def _validate_speed(self, speed: float) -> float:
        """Validate and normalize speed value"""
        if speed < VALID_SPEED_RANGE[0]:
            return VALID_SPEED_RANGE[0]
        elif speed > VALID_SPEED_RANGE[1]:
            return VALID_SPEED_RANGE[1]
        return speed

    def _validate_text(self, text: str) -> str:
        """Validate and clean text input"""
        if not text or not text.strip():
            raise ValueError("Text content cannot be empty")
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text.strip())
        
        # Check text length
        if len(text) > 1000:
            logger.warning(f"Text is very long ({len(text)} characters), may affect processing time")
        
        return text

    def _validate_audio_file(self, file_path: str) -> str:
        """Validate audio file path and format (supports both local files and URLs)"""
        if not file_path:
            raise ValueError("Audio file path cannot be empty")
        
        # Check if it's a URL
        if file_path.startswith(('http://', 'https://')):
            return self._validate_audio_url(file_path)
        else:
            return self._validate_local_audio_file(file_path)

    def _validate_local_audio_file(self, file_path: str) -> str:
        """Validate local audio file path and format"""
        if not os.path.exists(file_path):
            raise ValueError(f"Audio file not found: {file_path}")
        
        file_ext = os.path.splitext(file_path)[1].lower()
        if file_ext not in SUPPORTED_AUDIO_FORMATS:
            raise ValueError(f"Unsupported audio format: {file_ext}. Supported formats: {SUPPORTED_AUDIO_FORMATS}")
        
        # Check file size (reasonable limit)
        file_size = os.path.getsize(file_path)
        if file_size > 50 * 1024 * 1024:  # 50MB limit
            raise ValueError(f"Audio file too large: {file_size / (1024*1024):.1f}MB. Maximum size: 50MB")
        
        return file_path

    def _validate_audio_url(self, url: str) -> str:
        """Validate audio URL format"""
        from urllib.parse import urlparse
        
        try:
            # Parse URL to check if it's valid
            parsed_url = urlparse(url)
            if not parsed_url.scheme or not parsed_url.netloc:
                raise ValueError(f"Invalid URL format: {url}")
            
            # Check file extension from URL path if available
            url_path = parsed_url.path.lower()
            has_supported_ext = any(url_path.endswith(ext) for ext in SUPPORTED_AUDIO_FORMATS)
            
            if not has_supported_ext:
                logger.warning(f"Cannot determine audio format from URL: {url}. Will validate during download.")
            
            logger.info(f"Audio URL validation successful: {url}")
            return url
            
        except Exception as e:
            raise ValueError(f"URL validation failed: {str(e)}")

    async def _download_audio_from_url(self, url: str, user_id: str) -> str:
        """Download audio file from URL to local temporary file using DownloadUtils"""
        try:
            logger.info(f"Downloading reference audio from URL: {url}")
            
            # Create download utils instance
            downloader = DownloadUtils(user_id, max_file_size=50 * 1024 * 1024)  # 50MB limit
            
            # Download to temp subdirectory
            success, local_path, metadata = await downloader.download_file(
                url=url,
                subdir_type='temp',
                prefix='voice_ref',
                file_extension='.wav',  # Force WAV extension for voice cloning
                max_retries=3,
                timeout=60
            )
            
            if success:
                logger.info(f"Audio downloaded successfully to: {local_path}")
                logger.info(f"File size: {metadata.get('file_size_mb', 'unknown')} MB")
                
                # Validate the downloaded file
                self._validate_local_audio_file(local_path)
                
                return local_path
            else:
                error_msg = metadata.get('error', 'Unknown download error')
                raise ValueError(f"Failed to download audio from URL: {error_msg}")
                
        except Exception as e:
            logger.error(f"Error downloading audio from URL: {e}")
            raise ValueError(f"Failed to download audio: {str(e)}")

    async def clone_voice(self, text: str, reference_audio_path: str, voice_label: str, 
                                  speed: float = 1.0, user_id: str = None) -> Optional[Dict[str, Any]]:
        """Perform voice cloning and synthesis with URL download support"""
        temp_file_path = None
        download_metadata = None
        
        try:
            # Handle URL download if needed
            if reference_audio_path.startswith(('http://', 'https://')):
                temp_file_path = await self._download_audio_from_url(reference_audio_path, user_id or "voice_cloning_user")
                actual_audio_path = temp_file_path
                download_metadata = {
                    "source_type": "url",
                    "original_url": reference_audio_path,
                    "downloaded_to": temp_file_path
                }
            else:
                actual_audio_path = reference_audio_path
                download_metadata = {
                    "source_type": "local",
                    "local_path": reference_audio_path
                }
            
            # Step 1: Upload reference audio for cloning
            uploaded_voice_name = await self.upload_voice_for_cloning(actual_audio_path, voice_label)
            if not uploaded_voice_name:
                return None
            
            # Step 2: Synthesize speech with cloned voice
            params = {
                'text': text,
                'voice': uploaded_voice_name,
                'speed': speed
            }
            
            logger.info(f"Synthesizing speech with cloned voice: {uploaded_voice_name}")
            
            timeout = aiohttp.ClientTimeout(total=self.default_timeout)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self.api_headers
            ) as session:
                async with session.get(
                    f"{self.api_base_url}/voice-clone/synthesize_speech",
                    params=params
                ) as response:
                    
                    if response.status == 200:
                        # Check if response contains audio data
                        content_type = response.headers.get('content-type', '')
                        if content_type.startswith('audio/'):
                            audio_data = await response.read()
                            processing_time = response.headers.get('X-Elapsed-Time')
                            
                            result = {
                                "success": True,
                                "audio_data": audio_data,
                                "processing_time": processing_time,
                                "voice_type": "cloned_voice",
                                "voice_name": uploaded_voice_name,
                                "original_voice_label": voice_label,
                                "download_metadata": download_metadata
                            }
                            
                            return result
                        else:
                            logger.error("Cloned voice synthesis response is not audio data")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Cloned voice synthesis failed: HTTP {response.status} - {error_text}")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"Error in cloned voice synthesis: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("Cloned voice synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in cloned voice synthesis: {e}")
            return None
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logger.info(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")

    async def clone_voice_synthesis_v2(self, text: str, reference_audio_path: str, voice_label: str = None,
                                 model: str = "F5TTS_v1_Base", speed: float = 1.0, 
                                 remove_silence: bool = False, seed: int = -1, nfe_step: int = 32,
                                 user_id: str = None) -> Optional[Dict[str, Any]]:
        """Perform voice cloning synthesis using new v2 API with async processing"""
        temp_file_path = None
        download_metadata = None
        
        try:
            # Handle URL download if needed
            if reference_audio_path.startswith(('http://', 'https://')):
                temp_file_path = await self._download_audio_from_url(reference_audio_path, user_id or "voice_cloning_user")
                actual_audio_path = temp_file_path
                download_metadata = {
                    "source_type": "url",
                    "original_url": reference_audio_path,
                    "downloaded_to": temp_file_path
                }
            else:
                actual_audio_path = reference_audio_path
                download_metadata = {
                    "source_type": "local",
                    "local_path": reference_audio_path
                }

            # Step 1: Upload reference audio for cloning to get server path
            uploaded_voice_name = await self.upload_voice_for_cloning(actual_audio_path, voice_label)
            if not uploaded_voice_name:
                return {"success": False, "error": "Failed to upload reference audio file"}
            
            # Step 2: Prepare request payload for v2 API using JSON (not form data)
            request_data = {
                "ref_audio_orig": uploaded_voice_name,  # Server path to uploaded audio file
                "ref_text": "",  # Empty for auto-transcription
                "gen_text": text,
                "model": model,
                "remove_silence": remove_silence,
                "seed": seed,
                "cross_fade_duration": 0.15,
                "nfe_step": nfe_step,
                "speed": speed
            }

            logger.info(f"Starting voice cloning synthesis with model: {model}")
            logger.info(f"Using uploaded audio path: {uploaded_voice_name}")
            logger.info(f"Parameters: speed={speed}, remove_silence={remove_silence}, nfe_step={nfe_step}")

            # Submit synthesis task using JSON payload
            timeout = aiohttp.ClientTimeout(total=self.upload_timeout)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Authorization": self.api_headers.get("Authorization", ""),
                    "Content-Type": "application/json"
                }
            ) as session:
                
                # Submit synthesis request with JSON payload
                async with session.post(
                    f"{self.api_base_url}/voice-clone/synthesize_speech",
                    json=request_data  # Use json parameter instead of data
                ) as response:
                    
                    if response.status == 200:
                        submit_result = await response.json()
                        task_id = submit_result.get("task_id")
                        
                        if not task_id:
                            logger.error("API did not return task_id")
                            return {"success": False, "error": "API did not return task_id"}
                        
                        logger.info(f"Voice cloning task submitted successfully. Task ID: {task_id}")
                        
                        # Poll for result using AsyncTaskPoller
                        polling_result = await self.async_poller.polling_and_get_result(
                            task_id=task_id,
                            session_id=self.session_id,
                            tool_name=self.name,
                            engine_name="Voice Cloning",
                            max_poll_attempts=120,  # 120 * 5s = 10 minutes max
                            poll_interval=5.0,
                            extract_urls_callback=self._extract_audio_urls_from_response
                        )
                        
                        if polling_result.get("success"):
                            # Add download metadata to result
                            result = polling_result.copy()
                            result.update({
                                "voice_type": "cloned_voice",
                                "original_voice_label": voice_label,
                                "uploaded_voice_path": uploaded_voice_name,
                                "download_metadata": download_metadata,
                                "synthesis_parameters": {
                                    "model": model,
                                    "speed": speed,
                                    "remove_silence": remove_silence,
                                    "seed": seed,
                                    "nfe_step": nfe_step
                                }
                            })
                            return result
                        else:
                            return polling_result
                
                    else:
                        error_text = await response.text()
                        logger.error(f"Voice cloning submission failed: HTTP {response.status} - {error_text}")
                        return {"success": False, "error": f"HTTP {response.status}: {error_text}"}

        except aiohttp.ClientError as e:
            logger.error(f"Error in voice cloning synthesis: {e}")
            return {"success": False, "error": f"Client error: {str(e)}"}
        except asyncio.TimeoutError:
            logger.error("Voice cloning synthesis timed out")
            return {"success": False, "error": "Request timed out"}
        except Exception as e:
            logger.error(f"Unexpected error in voice cloning synthesis: {e}")
            return {"success": False, "error": f"Unexpected error: {str(e)}"}
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logger.info(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")

    def _extract_audio_urls_from_response(self, response: Dict[str, Any]) -> Dict[str, List[str]]:
        """Extract audio URLs from the API response for AsyncTaskPoller"""
        local_urls, s3_urls = [], []
        
        # Handle different response structures
        target = response.get('result', response)
        
        if isinstance(target, dict):
            # Check for direct audio URL
            if target.get('audio_url'):
                local_urls.append(target['audio_url'])
            
            # Check for S3 share URL
            if target.get('s3_share_url'):
                s3_urls.append(target['s3_share_url'])
            
            # Check for other possible URL fields
            for url_field in ['output_url', 'file_url', 'download_url']:
                if target.get(url_field):
                    local_urls.append(target[url_field])
                    break

        all_urls = local_urls + s3_urls
        logger.info(f"Extracted audio URLs: {len(all_urls)} total ({len(local_urls)} local, {len(s3_urls)} S3)")
        
        return {
            'all_urls': all_urls,
            'local_urls': local_urls,
            's3_urls': s3_urls
        }

    async def execute(
        self,
        text: str,
        mode: str = "base_tts",
        voice_name: Optional[str] = None,
        reference_audio_path: Optional[str] = None,
        voice_label: Optional[str] = None,
        model: Optional[str] = "F5TTS_v1_Base",
        speed: Optional[float] = 1.0,
        remove_silence: Optional[bool] = False,
        seed: Optional[int] = -1,
        nfe_step: Optional[int] = 32
    ) -> ToolResult:
        """Execute voice cloning and synthesis with credits tracking"""
        
        try:
            logger.info(f"VoiceCloning.execute called with mode: {mode}, model: {model}, text: {text[:100]}...")
            
            # Handle list_voices mode
            if mode == "list_voices":
                voices_list = self._format_demo_voices_list()
                return ToolResult(
                    output=voices_list,
                    metadata={
                        "available_voices": DEFAULT_DEMO_VOICES,
                        "supported_modes": ["base_tts", "existing_voice", "clone_voice"],
                        "supported_models": ["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base"],
                        "action_required": "mode_selection"
                    }
                )
            
            # Validate common parameters
            validated_text = self._validate_text(text)
            validated_speed = self._validate_speed(speed or 1.0)
            
            # Update auth headers if user_id is provided
            if self.user_id:
                api_key = generate_token_direct(self.user_id)
                self.api_headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": "e-ManusWeb/1.0"
                }
                # Update async poller with new headers
                api_endpoints = {
                    "base_url": self.api_base_url,
                    "status": f"{self.api_base_url}/voice-clone/status/"+"{task_id}",
                    "result": f"{self.api_base_url}/voice-clone/result/"+"{task_id}"
                }
                self.async_poller = AsyncTaskPoller(api_endpoints, self.api_headers)
                logger.info(f"Updated auth token for user: {self.user_id}")
            
            logger.info(f"Validated parameters - mode: {mode}, model: {model}, speed: {validated_speed}")
            
            # Execute based on mode - now all modes use the new v2 API
            synthesis_result = None
            
            if mode == "clone_voice":
                if not reference_audio_path:
                    return ToolResult(error="❌ clone_voice模式需要提供reference_audio_path参数（本地路径或URL）。")
                
                if not voice_label:
                    # Generate default voice label
                    voice_label = f"custom_voice_{self.user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                
                # Validate audio file (supports both local and URL)
                validated_audio_path = self._validate_audio_file(reference_audio_path)
                
                synthesis_result = await self.clone_voice_synthesis_v2(
                    text=validated_text,
                    reference_audio_path=validated_audio_path,
                    voice_label=voice_label,
                    model=model or "F5TTS_v1_Base",
                    speed=validated_speed,
                    remove_silence=remove_silence or False,
                    seed=seed or -1,
                    nfe_step=nfe_step or 32,
                    user_id=self.user_id
                )
                
            else:
                # For base_tts and existing_voice, we can use a simplified approach or fallback
                return ToolResult(error=f"❌ 模式 '{mode}' 暂时不支持新的v2 API。请使用 'clone_voice' 模式。")
            
            # Process synthesis result
            if synthesis_result and synthesis_result.get("success"):
                # Get audio URLs from result
                result_urls = synthesis_result.get('result_urls', {})
                local_urls = result_urls.get('local_urls', [])
                s3_urls = result_urls.get('s3_urls', [])
                
                # Build success message
                output_lines = ["✅ 语音合成成功！\n"]
                
                output_lines.append(f"🎵 生成的语音文件:")
                if local_urls:
                    output_lines.append(f"   📁 本地文件: {local_urls[0]}")
                if s3_urls:
                    output_lines.append(f"   🌐 下载链接: {s3_urls[0]}")
                
                output_lines.append(f"\n📝 输入文本: {validated_text[:100]}{'...' if len(validated_text) > 100 else ''}")
                output_lines.append(f"🎯 合成模式: {mode}")
                output_lines.append(f"🤖 使用模型: {model}")
                output_lines.append(f"⚡ 语速: {validated_speed}x")
                
                # Add synthesis parameters
                synthesis_params = synthesis_result.get("synthesis_parameters", {})
                if synthesis_params.get("remove_silence"):
                    output_lines.append(f"🔇 静音移除: 已启用")
                if synthesis_params.get("seed", -1) != -1:
                    output_lines.append(f"🎲 随机种子: {synthesis_params['seed']}")
                output_lines.append(f"🔧 计算步数: {synthesis_params.get('nfe_step', 32)}")
                
                if synthesis_result.get("original_voice_label"):
                    output_lines.append(f"🏷️ 声音标签: {synthesis_result['original_voice_label']}")
                
                # Add download info for URL-based reference audio
                download_metadata = synthesis_result.get("download_metadata")
                if download_metadata and download_metadata.get("source_type") == "url":
                    output_lines.append(f"📥 参考音频: 从URL下载")
                    output_lines.append(f"   🌐 原始URL: {download_metadata['original_url']}")
                elif download_metadata and download_metadata.get("source_type") == "local":
                    output_lines.append(f"📁 参考音频: 本地文件")
                
                if synthesis_result.get("processing_time"):
                    output_lines.append(f"⏱️ 处理时间: {synthesis_result['processing_time']}秒")
                
                # Create successful ToolResult with credits
                tool_result = ToolResult(
                    output="\n".join(output_lines),
                    metadata={
                        'audio_urls': local_urls + s3_urls,
                        'local_urls': local_urls,
                        's3_urls': s3_urls,
                        'primary_audio_url': s3_urls[0] if s3_urls else (local_urls[0] if local_urls else None),
                        'input_text': validated_text,
                        'mode': mode,
                        'model': model,
                        'speed': validated_speed,
                        'synthesis_parameters': synthesis_params,
                        'user_id': self.user_id,
                        'processing_time': synthesis_result.get('processing_time'),
                        'reference_audio_source': download_metadata.get('source_type') if download_metadata else None,
                        'reference_audio_url': download_metadata.get('original_url') if download_metadata else None
                    }
                )
                
                # Calculate credits based on mode and complexity
                text_length = len(validated_text)
                
                if mode == "clone_voice":
                    # Voice cloning is premium complexity
                    audio_credits = CreditsManager.calculate_audio_credits(
                        text_length=text_length,
                        complexity='premium'
                    )
                    # Add extra credits for voice cloning process
                    cloning_credits = credits_config_manager.get_credits('image_generation', complexity='premium')
                    tool_result.add_credits("voice_cloning", cloning_credits)
                    
                    # Add extra credits for URL download if applicable
                    if download_metadata and download_metadata.get("source_type") == "url":
                        download_credits = credits_config_manager.get_credits('text_expansion', multiplier=0.5)
                        tool_result.add_credits("url_download", download_credits)
                    
                    # Add model complexity credits
                    model_multiplier = {"F5TTS_v1_Base": 1.0, "F5TTS_Base": 1.2, "E2TTS_Base": 1.5}.get(model, 1.0)
                    if model_multiplier != 1.0:
                        model_credits = int(audio_credits * (model_multiplier - 1.0))
                        tool_result.add_credits("model_complexity", model_credits)
                
                else:
                    # Other modes use standard credits
                    audio_credits = CreditsManager.calculate_audio_credits(
                        text_length=text_length,
                        complexity='medium'
                    )
                
                tool_result.add_credits("voice_synthesis", audio_credits)
                
                # Add speed adjustment credits if speed is not default
                if validated_speed != 1.0:
                    speed_credits = credits_config_manager.get_credits('text_expansion', multiplier=0.3)
                    tool_result.add_credits("speed_adjustment", speed_credits)
                
                logger.info(f"🎤 Voice synthesis completed. {tool_result.get_credits_summary()}")
                return tool_result
                
            else:
                # Synthesis failed
                error_message = synthesis_result.get('error', '未知错误') if synthesis_result else '合成失败'
                error_message = f"❌ 语音合成失败: {error_message}"
                logger.error(error_message)
                
                tool_result = ToolResult(
                    error=error_message,
                    metadata={
                        'input_text': validated_text,
                        'mode': mode,
                        'model': model,
                        'synthesis_failed': True
                    }
                )
                
                # Add minimal credits for failed attempts
                failure_credits = 5
                tool_result.add_credits("failed_voice_synthesis", failure_credits)
                
                return tool_result
                
        except ValueError as e:
            error_message = f"❌ 输入验证失败: {str(e)}"
            logger.error(error_message)
            return ToolResult(error=error_message)  # No credits for validation errors
            
        except Exception as e:
            error_message = f"❌ 语音合成过程中发生意外错误: {str(e)}"
            logger.error(f"Unexpected error in voice cloning execution: {e}", exc_info=True)
            
            tool_result = ToolResult(
                error=error_message,
                metadata={'input_text': text, 'mode': mode, 'model': model}
            )
            
            # Add minimal credits for unexpected errors
            error_credits = 3
            tool_result.add_credits("error_voice_synthesis", error_credits)
            
            return tool_result

    def get_usage_example(self) -> str:
        """Return usage example for this tool"""
        return """
Example usage:

List available voices:
```python
voice_cloning_tool = VoiceCloning()
result = await voice_cloning_tool.execute(
    text="Hello world",
    mode="list_voices"
)
```

Base TTS (default voice):
```python
result = await voice_cloning_tool.execute(
    text="Hello, this is a test of the voice synthesis system.",
    mode="base_tts",
    speed=1.0
)
```

Existing voice synthesis:
```python
result = await voice_cloning_tool.execute(
    text="Hello, this is a test with demo voice.",
    mode="existing_voice",
    voice_name="demo_speaker0",
    speed=1.2
)
```

Voice cloning with local file:
```python
result = await voice_cloning_tool.execute(
    text="Hello, this is a test with cloned voice.",
    mode="clone_voice",
    reference_audio_path="/path/to/reference.wav",
    voice_label="my_custom_voice",
    speed=1.0
)
```

Voice cloning with URL:
```python
result = await voice_cloning_tool.execute(
    text="Hello, this is a test with cloned voice from URL.",
    mode="clone_voice",
    reference_audio_path="https://example.com/reference_audio.wav",
    voice_label="web_voice",
    speed=1.0
)
```

Required parameters:
- text: Text content to convert to speech
- mode: Synthesis mode (base_tts/existing_voice/clone_voice/list_voices)

Optional parameters:
- voice_name: Voice name for existing_voice mode
- reference_audio_path: Audio file path or URL for clone_voice mode
- voice_label: Custom voice label for clone_voice mode
- speed: Speech speed from 0.5 to 2.0 (default: 1.0)

The tool will return:
- Generated audio file URL with synthesis details
- Local file path for downloaded audio
- Processing time and file size information
- Credits usage summary
- Download metadata for URL-based reference audio
"""

    def get_api_info(self) -> Dict[str, Any]:
        """Get API configuration information"""
        return {
            "base_url": self.api_base_url,
            "timeout": self.default_timeout,
            "upload_timeout": self.upload_timeout,
            "has_auth": bool(self.api_headers.get("Authorization")),
            "supported_modes": ["base_tts", "existing_voice", "clone_voice", "list_voices"],
            "supported_formats": SUPPORTED_AUDIO_FORMATS,
            "speed_range": VALID_SPEED_RANGE,
            "demo_voices": DEFAULT_DEMO_VOICES,
            "max_file_size": "50MB"
        }

    async def check_api_status(self) -> Dict[str, Any]:
        """Check if the Voice Cloning API is accessible"""
        try:
            # Test base TTS endpoint with simple request
            timeout = aiohttp.ClientTimeout(total=10)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self.api_headers
            ) as session:
                async with session.get(
                    f"{self.api_base_url}/base_tts/",
                    params={"text": "test", "speed": 1.0}
                ) as response:
                    
                    if response.status == 200:
                        return {"status": "online", "message": "API is accessible"}
                    else:
                        error_text = await response.text()
                        return {"status": "error", "message": f"API returned status {response.status}: {error_text}"}
                        
        except aiohttp.ClientError as e:
            return {"status": "offline", "message": f"API is not accessible: {str(e)}"}
        except asyncio.TimeoutError:
            return {"status": "offline", "message": "API connection timed out"}
        except Exception as e:
            return {"status": "error", "message": f"Unexpected error: {str(e)}"}

    def _format_demo_voices_list(self) -> str:
        """Format demo voices list and model information for user selection"""
        output_lines = []
        output_lines.append("🎤 语音合成信息：\n")
        
        output_lines.append("🤖 可用模型：")
        output_lines.append("  1. `F5TTS_v1_Base` - F5-TTS v1 基础模型（推荐）")
        output_lines.append("  2. `F5TTS_Base` - F5-TTS 基础模型")
        output_lines.append("  3. `E2TTS_Base` - E2-TTS 基础模型\n")
        
        output_lines.append("🎯 支持的合成模式：")
        output_lines.append("  • `clone_voice` - 克隆声音（需要参考音频）")
        output_lines.append("    - reference_audio_path: 参考音频文件路径或URL")
        output_lines.append("    - voice_label: 自定义声音标签")
        output_lines.append("    - model: 选择AI模型（默认: F5TTS_v1_Base）")
        output_lines.append("    - speed: 语速 0.5-2.0（默认: 1.0）")
        output_lines.append("    - remove_silence: 移除静音（默认: false）")
        output_lines.append("    - seed: 随机种子（默认: -1）")
        output_lines.append("    - nfe_step: 计算步数（默认: 32）")
        
        output_lines.append("\n💡 使用示例：")
        output_lines.append("- 克隆声音：mode='clone_voice', reference_audio_path='path/to/audio.wav'")
        
        return "\n".join(output_lines)

    async def base_tts_synthesis(self, text: str, speed: float = 1.0) -> Optional[Dict[str, Any]]:
        """Perform base TTS synthesis with default voice"""
        try:
            params = {
                'text': text,
                'speed': speed
            }
            
            logger.info(f"Performing base TTS synthesis with speed: {speed}")
            
            timeout = aiohttp.ClientTimeout(total=self.default_timeout)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self.api_headers
            ) as session:
                async with session.get(
                    f"{self.api_base_url}/base_tts/",
                    params=params
                ) as response:
                    
                    if response.status == 200:
                        # Check if response contains audio data
                        content_type = response.headers.get('content-type', '')
                        if content_type.startswith('audio/'):
                            audio_data = await response.read()
                            processing_time = response.headers.get('X-Elapsed-Time')
                            
                            return {
                                "success": True,
                                "audio_data": audio_data,
                                "processing_time": processing_time,
                                "voice_type": "base_tts"
                            }
                        else:
                            logger.error("Base TTS response is not audio data")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Base TTS failed: HTTP {response.status} - {error_text}")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"Error in base TTS synthesis: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("Base TTS synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in base TTS synthesis: {e}")
            return None

    async def existing_voice_synthesis(self, text: str, voice_name: str, speed: float = 1.0) -> Optional[Dict[str, Any]]:
        """Perform synthesis with existing voice"""
        try:
            params = {
                'text': text,
                'voice': voice_name,
                'speed': speed
            }
            
            logger.info(f"Performing synthesis with existing voice: {voice_name}, speed: {speed}")
            
            timeout = aiohttp.ClientTimeout(total=self.default_timeout)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self.api_headers
            ) as session:
                async with session.get(
                    f"{self.api_base_url}/voice-clone/synthesize_speech",
                    params=params
                ) as response:
                    
                    if response.status == 200:
                        # Check if response contains audio data
                        content_type = response.headers.get('content-type', '')
                        if content_type.startswith('audio/'):
                            audio_data = await response.read()
                            processing_time = response.headers.get('X-Elapsed-Time')
                            
                            return {
                                "success": True,
                                "audio_data": audio_data,
                                "processing_time": processing_time,
                                "voice_type": "existing_voice",
                                "voice_name": voice_name
                            }
                        else:
                            logger.error("Existing voice synthesis response is not audio data")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Existing voice synthesis failed: HTTP {response.status} - {error_text}")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"Error in existing voice synthesis: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("Existing voice synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in existing voice synthesis: {e}")
            return None

    async def upload_voice_for_cloning(self, audio_file_path: str, voice_label: str) -> Optional[str]:
        """Upload audio file for voice cloning and return voice name"""
        try:
            logger.info(f"Uploading audio file for voice cloning: {audio_file_path}")
            
            timeout = aiohttp.ClientTimeout(total=self.upload_timeout)
            
            # Create headers without Content-Type for multipart (aiohttp will set it automatically)
            upload_headers = {k: v for k, v in self.api_headers.items() if k.lower() != "content-type"}
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=upload_headers
            ) as session:
                
                # Prepare multipart form data
                form_data = aiohttp.FormData()
                form_data.add_field('audio_file_label', voice_label)
                
                # Add file - need to read the file content first
                with open(audio_file_path, 'rb') as audio_file:
                    file_content = audio_file.read()
                
                form_data.add_field(
                    'file',
                    file_content,
                    filename=os.path.basename(audio_file_path),
                    content_type='audio/wav'
                )
                
                # Try both possible endpoints
                upload_endpoint = f"{self.api_base_url}/voice-clone/upload_audio"

                try:
                    logger.info(f"Trying upload endpoint: {upload_endpoint}")
                    async with session.post(
                        upload_endpoint,
                        data=form_data
                    ) as response:
                        
                        if response.status == 200:
                            result = await response.json()
                            
                            # Check for success indicators in the response
                            if (result.get('message') and 'successful' in result.get('message', '').lower()) or \
                                result.get('status') == 'success' or \
                                result.get('voice_name'):
                                
                                actual_voice_name = result.get('voice_name', voice_label)
                                logger.info(f"Voice upload successful! Unique voice name: {actual_voice_name}")
                                logger.info(f"Upload endpoint used: {upload_endpoint}")
                                return actual_voice_name
                            else:
                                logger.warning(f"Voice upload response unclear: {result}")
                        
                        elif response.status == 404:
                            # Endpoint not found, try next one
                            logger.warning(f"Endpoint not found: {upload_endpoint}")
                        
                        else:
                            error_text = await response.text()
                            logger.error(f"Voice upload failed at {upload_endpoint}: HTTP {response.status} - {error_text}")
                            # Continue to next endpoint
                        
                                
                except aiohttp.ClientError as e:
                    logger.warning(f"Client error with endpoint {upload_endpoint}: {e}")
                    
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout with endpoint {upload_endpoint}")
                    
            
            # If we get here, all endpoints failed
            logger.error("All upload endpoints failed")
            return None
                        
        except Exception as e:
            logger.error(f"Unexpected error uploading voice: {e}")
            return None

    async def clone_voice_synthesis(self, text: str, reference_audio_path: str, voice_label: str, 
                                  speed: float = 1.0, user_id: str = None) -> Optional[Dict[str, Any]]:
        """Perform voice cloning and synthesis with URL download support"""
        temp_file_path = None
        download_metadata = None
        
        try:
            # Handle URL download if needed
            if reference_audio_path.startswith(('http://', 'https://')):
                temp_file_path = await self._download_audio_from_url(reference_audio_path, user_id or "voice_cloning_user")
                actual_audio_path = temp_file_path
                download_metadata = {
                    "source_type": "url",
                    "original_url": reference_audio_path,
                    "downloaded_to": temp_file_path
                }
            else:
                actual_audio_path = reference_audio_path
                download_metadata = {
                    "source_type": "local",
                    "local_path": reference_audio_path
                }
            
            # Step 1: Upload reference audio for cloning
            uploaded_voice_name = await self.upload_voice_for_cloning(actual_audio_path, voice_label)
            if not uploaded_voice_name:
                return None
            
            # Step 2: Synthesize speech with cloned voice
            params = {
                'text': text,
                'voice': uploaded_voice_name,
                'speed': speed
            }
            
            logger.info(f"Synthesizing speech with cloned voice: {uploaded_voice_name}")
            
            timeout = aiohttp.ClientTimeout(total=self.default_timeout)
            
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=self.api_headers
            ) as session:
                async with session.get(
                    f"{self.api_base_url}/voice-clone/synthesize_speech",
                    params=params
                ) as response:
                    
                    if response.status == 200:
                        # Check if response contains audio data
                        content_type = response.headers.get('content-type', '')
                        if content_type.startswith('audio/'):
                            audio_data = await response.read()
                            processing_time = response.headers.get('X-Elapsed-Time')
                            
                            result = {
                                "success": True,
                                "audio_data": audio_data,
                                "processing_time": processing_time,
                                "voice_type": "cloned_voice",
                                "voice_name": uploaded_voice_name,
                                "original_voice_label": voice_label,
                                "download_metadata": download_metadata
                            }
                            
                            return result
                        else:
                            logger.error("Cloned voice synthesis response is not audio data")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Cloned voice synthesis failed: HTTP {response.status} - {error_text}")
                        return None
                        
        except aiohttp.ClientError as e:
            logger.error(f"Error in cloned voice synthesis: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("Cloned voice synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in cloned voice synthesis: {e}")
            return None
        finally:
            # Clean up temporary file if it was created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                    logger.info(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file {temp_file_path}: {e}")

    def _save_audio_file(self, audio_data: bytes, mode: str, voice_info: Dict[str, Any], user_id: str) -> Optional[Dict[str, str]]:
        """Save audio data to user-specific workspace and return file information"""
        try:
            # Generate unique filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            voice_name = voice_info.get('voice_name', 'unknown')
            voice_type = voice_info.get('voice_type', mode)
            
            # Clean voice_name for filename
            clean_voice_name = "".join(c for c in voice_name if c.isalnum() or c in '_-')
            
            filename = f"voice_{voice_type}_{clean_voice_name}_{timestamp}.wav"
            
            # Get user-specific audios directory
            audios_dir = self.get_workspace_directory(user_id, "audios")
            local_filepath = audios_dir / filename
            
            # Save audio data
            with open(local_filepath, 'wb') as f:
                f.write(audio_data)
            
            # Generate public URL with user isolation
            public_url = self.generate_user_public_file_url(user_id, filename, "audios")
            
            file_size = len(audio_data)
            file_size_mb = file_size / (1024 * 1024)
            
            logger.info(f"Audio file saved to: {local_filepath}")
            logger.info(f"File size: {file_size_mb:.2f} MB")
            logger.info(f"Public URL: {public_url}")
            
            return {
                "local_path": str(local_filepath),
                "filename": filename,
                "public_url": public_url,
                "file_size": file_size,
                "file_size_mb": round(file_size_mb, 2)
            }
            
        except Exception as e:
            logger.error(f"Error saving audio file: {e}")
            return None

    def generate_user_public_file_url(self, user_id: str, filename: str, file_type: str = "audios") -> str:
        """Generate public accessible URL for a user-specific file"""
        try:
            # Get public file base URL from config
            voice_cloning_config = getattr(config, 'voice_cloning_config', None)
            if voice_cloning_config and hasattr(voice_cloning_config, 'public_file_base_url'):
                base_url = voice_cloning_config.public_file_base_url
            else:
                # Try general config
                general_config = getattr(config, 'general', None)
                if general_config and hasattr(general_config, 'public_file_base_url'):
                    base_url = general_config.public_file_base_url
                else:
                    # Fallback default
                    base_url = 'https://emanus.aiworm.cn/api/files'
        
            # Generate user-specific public URL
            public_url = f"{base_url}/users/{user_id}/{file_type}/{filename}"
            
            logger.info(f"Generated user public URL: {public_url}")
            return public_url
            
        except Exception as e:
            logger.error(f"Error generating user public URL: {e}")
            # Fallback to simple URL
            return f"https://emanus.aiworm.cn/api/files/{file_type}/{filename}"

    def get_workspace_directory(self, user_id: str, subdir: str = "audios") -> Path:
        """Get user-specific workspace directory"""
        try:
            # Get workspace base from config
            general_config = getattr(config, 'general', None)
            if general_config and hasattr(general_config, 'storage_base'):
                workspace_base = general_config.storage_base
            else:
                workspace_base = "/app/e-ManusWeb/workspace"
            
            # Create user-specific directory path
            user_dir = Path(workspace_base) / "users" / user_id / subdir
            
            # Ensure directory exists
            user_dir.mkdir(parents=True, exist_ok=True)
            
            return user_dir
            
        except Exception as e:
            logger.error(f"Error getting workspace directory for user {user_id}: {e}")
            # Fallback
            fallback_dir = Path(f"./workspace/users/{user_id}/{subdir}")
            fallback_dir.mkdir(parents=True, exist_ok=True)
            return fallback_dir
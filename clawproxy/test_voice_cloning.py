#!/usr/bin/env python3
"""
Test script for F5-TTS voice cloning functionality with authentication.
This script demonstrates how to:
1. Upload a reference audio file to clone a voice (with auth)
2. Use the cloned voice to synthesize new speech (with auth)
"""

import requests
import os
import sys
import time
from pathlib import Path

# Server configuration
SERVER_URL = "http://localhost:8000"
OUTPUT_DIR = "test_outputs"

# Authentication token - replace with your actual token
AUTH_TOKEN = "token1"  # Default test token

def get_auth_headers():
    """Get authentication headers for requests."""
    return {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Content-Type": "application/json"
    }

def get_auth_headers_multipart():
    """Get authentication headers for multipart requests (without Content-Type)."""
    return {
        "Authorization": f"Bearer {AUTH_TOKEN}"
    }

def ensure_output_dir():
    """Create output directory if it doesn't exist."""
    Path(OUTPUT_DIR).mkdir(exist_ok=True)

def test_voice_cloning(reference_audio_path, text_to_synthesize, voice_name="test_voice"):
    """
    Test voice cloning by uploading a reference audio and synthesizing speech.
    
    Args:
        reference_audio_path (str): Path to the reference audio file (.wav)
        text_to_synthesize (str): Text to convert to speech using the cloned voice
        voice_name (str): Name for the cloned voice
    """
    
    print(f"🎤 Testing F5-TTS Voice Cloning with Authentication")
    print(f"🔐 Using token: {AUTH_TOKEN}")
    print(f"📁 Reference audio: {reference_audio_path}")
    print(f"📝 Text to synthesize: {text_to_synthesize}")
    print(f"🔊 Voice name: {voice_name}")
    print("-" * 50)
    
    # Check if reference audio file exists
    if not os.path.exists(reference_audio_path):
        print(f"❌ Error: Reference audio file not found: {reference_audio_path}")
        return False
    
    # Step 1: Upload reference audio file to clone the voice
    print("📤 Step 1: Uploading reference audio for voice cloning...")
    
    try:
        with open(reference_audio_path, 'rb') as audio_file:
            files = {'file': (os.path.basename(reference_audio_path), audio_file, 'audio/wav')}
            data = {'audio_file_label': voice_name}
            
            # ✅ UPDATED: Add authentication headers for multipart request
            response = requests.post(
                f"{SERVER_URL}/voice-clone/upload_audio",
                files=files,
                data=data,
                headers=get_auth_headers_multipart(),
                timeout=120  # Allow time for processing
            )
            
            if response.status_code == 200:
                result = response.json()
                print(f"✅ Voice upload successful! {result.get('message', 'Voice uploaded')}")
                
                # Get the actual unique voice name returned by the server
                actual_voice_name = result.get('voice_name', voice_name)
                print(f"🎯 Unique voice name: {actual_voice_name}")
                
                # Update the voice name to use the server-generated unique name
                voice_name = actual_voice_name
            elif response.status_code == 401:
                print(f"❌ Authentication failed: Invalid or missing token")
                print(f"🔑 Make sure AUTH_TOKEN is set correctly: {AUTH_TOKEN}")
                return False
            else:
                print(f"❌ Voice upload failed: {response.status_code} - {response.text}")
                return False
                
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error during voice upload: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error during voice upload: {e}")
        return False
    
    # Step 2: Synthesize speech using the cloned voice
    print(f"🎯 Step 2: Synthesizing speech with cloned voice '{voice_name}'...")
    
    try:
        # ✅ V2: Use POST endpoint with JSON payload
        payload = {
            'ref_audio_orig': voice_name,
            'gen_text': text_to_synthesize,
            'ref_text': '',
            'model': 'F5TTS_v1_Base',
            'remove_silence': False,
            'seed': -1,
            'cross_fade_duration': 0.15,
            'nfe_step': 32,
            'speed': 1.0
        }
        
        response = requests.post(
            f"{SERVER_URL}/voice-clone/synthesize_speech?need_credit=false",  # Query parameter
            json=payload,
            headers=get_auth_headers(),
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            task_id = result.get('task_id')
            print(f"✅ Synthesis task started! Task ID: {task_id}")
            
            # Poll for task completion by checking status
            max_wait = 180
            wait_interval = 2
            elapsed = 0
            
            while elapsed < max_wait:
                time.sleep(wait_interval)
                elapsed += wait_interval
                
                # Check task status
                status_response = requests.get(
                    f"{SERVER_URL}/voice-clone/status/{task_id}",
                    headers=get_auth_headers(),
                    timeout=30
                )
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    # The response has structure: {success: bool, task: {...}, message: str}
                    task_info = status_data.get('task', {})
                    status = task_info.get('status')
                    progress = int((task_info.get('progress', 0) or 0) * 100)  # Convert 0.0-1.0 to percentage
                    
                    print(f"⏳ Status: {status} - Progress: {progress}%")
                    
                    # Check if task is in terminal state
                    if status in ['succeeded', 'completed']:
                        # Task completed successfully, get the result
                        print(f"✅ Task completed! Getting result...")
                        result_response = requests.get(
                            f"{SERVER_URL}/voice-clone/result/{task_id}",
                            headers=get_auth_headers(),
                            timeout=30
                        )
                        
                        if result_response.status_code == 200:
                            output_file = os.path.join(OUTPUT_DIR, f"synthesized_{voice_name}.wav")
                            with open(output_file, 'wb') as f:
                                f.write(result_response.content)
                            
                            print(f"✅ Speech synthesis successful!")
                            print(f"🔊 Output saved to: {output_file}")
                            
                            # Display info from headers
                            if 'X-Processing-Time' in result_response.headers:
                                print(f"⏱️  Processing time: {result_response.headers['X-Processing-Time']} seconds")
                            if 'X-Credits-Consumed' in result_response.headers:
                                print(f"💳 Credits consumed: {result_response.headers['X-Credits-Consumed']}")
                            
                            return True
                        else:
                            print(f"❌ Failed to get result: {result_response.status_code}")
                            return False
                    
                    elif status in ['failed', 'cancelled', 'timeout']:
                        # Task failed or was cancelled
                        error = task_info.get('error_message') or status_data.get('message', 'Unknown error')
                        print(f"❌ Task {status}: {error}")
                        return False
                    
                    # Task still processing, continue polling
                else:
                    print(f"⚠️ Failed to get status: {status_response.status_code}")
            
            print(f"❌ Timeout waiting for synthesis to complete")
            return False
            
        elif response.status_code == 401:
            print(f"❌ Authentication failed: Invalid or missing token")
            return False
        else:
            print(f"❌ Speech synthesis failed: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error during speech synthesis: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error during speech synthesis: {e}")
        return False

def test_with_existing_voice(text_to_synthesize, voice_name="demo_speaker0"):
    """
    Test speech synthesis with an existing voice (no upload needed).
    
    Args:
        text_to_synthesize (str): Text to convert to speech
        voice_name (str): Name of existing voice
    """
    
    print(f"🎯 Testing speech synthesis with existing voice '{voice_name}'...")
    print(f"🔐 Using authentication token: {AUTH_TOKEN}")
    
    try:
        payload = {
            'ref_audio_orig': f"resources/{voice_name}.wav",
            'gen_text': text_to_synthesize,
            'ref_text': '',
            'model': 'F5TTS_v1_Base',
            'speed': 1.0
        }
        
        # ✅ UPDATED: Use v2 POST endpoint with query parameter
        response = requests.post(
            f"{SERVER_URL}/voice-clone/synthesize_speech?need_credit=false",  # Query parameter
            json=payload,
            headers=get_auth_headers(),
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            task_id = result.get('task_id')
            print(f"✅ Task started: {task_id}")
            
            # Poll for task completion by checking status
            time.sleep(3)  # Initial wait
            
            for attempt in range(60):
                # Check task status first
                status_response = requests.get(
                    f"{SERVER_URL}/voice-clone/status/{task_id}",
                    headers=get_auth_headers(),
                    timeout=30
                )
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    # The response has structure: {success: bool, task: {...}, message: str}
                    task_info = status_data.get('task', {})
                    status = task_info.get('status')
                    progress = int((task_info.get('progress', 0) or 0) * 100)  # Convert 0.0-1.0 to percentage
                    
                    if attempt % 5 == 0:  # Print status every 5 attempts
                        print(f"⏳ Status: {status} - Progress: {progress}%")
                    
                    # Check if task completed
                    if status in ['succeeded', 'completed']:
                        # Get the result
                        result_response = requests.get(
                            f"{SERVER_URL}/voice-clone/result/{task_id}",
                            headers=get_auth_headers(),
                            timeout=30
                        )
                        
                        if result_response.status_code == 200:
                            output_file = os.path.join(OUTPUT_DIR, f"existing_voice_{voice_name}.wav")
                            with open(output_file, 'wb') as f:
                                f.write(result_response.content)
                            
                            print(f"✅ Speech synthesis successful with existing voice!")
                            print(f"🔊 Output saved to: {output_file}")
                            
                            if 'X-Credits-Consumed' in result_response.headers:
                                print(f"💳 Credits consumed: {result_response.headers['X-Credits-Consumed']}")
                            
                            return True
                    
                    elif status in ['failed', 'cancelled', 'timeout']:
                        error = task_info.get('error_message') or status_data.get('message', 'Unknown error')
                        print(f"❌ Task {status}: {error}")
                        return False
                    
                    # Still processing, wait and continue
                    time.sleep(3)
                else:
                    print(f"⚠️ Status check failed: {status_response.status_code}")
                    time.sleep(3)
            
            print(f"❌ Timeout waiting for result")
            return False
        elif response.status_code == 401:
            print(f"❌ Authentication failed: Invalid or missing token")
            return False
        else:
            print(f"❌ Speech synthesis failed: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_base_tts(text_to_synthesize):
    """
    Test the base TTS endpoint (uses default English voice).
    Note: v2 server doesn't have /base_tts, using default voice instead.
    
    Args:
        text_to_synthesize (str): Text to convert to speech
    """
    
    print(f"🎯 Testing synthesis with default voice (v2 server)...")
    print(f"🔐 Using authentication token: {AUTH_TOKEN}")
    
    try:
        payload = {
            'ref_audio_orig': 'resources/default_en.wav',
            'gen_text': text_to_synthesize,
            'ref_text': '',
            'model': 'F5TTS_v1_Base',
            'speed': 1.0
        }
        
        response = requests.post(
            f"{SERVER_URL}/voice-clone/synthesize_speech?need_credit=false",  # Query parameter
            json=payload,
            headers=get_auth_headers(),
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            task_id = result.get('task_id')
            print(f"✅ Task started: {task_id}")
            
            # Poll for task completion by checking status
            time.sleep(3)
            
            for attempt in range(60):
                # Check task status first
                status_response = requests.get(
                    f"{SERVER_URL}/voice-clone/status/{task_id}",
                    headers=get_auth_headers(),
                    timeout=30
                )
                
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    # The response has structure: {success: bool, task: {...}, message: str}
                    task_info = status_data.get('task', {})
                    status = task_info.get('status')
                    progress = int((task_info.get('progress', 0) or 0) * 100)  # Convert 0.0-1.0 to percentage
                    
                    if attempt % 5 == 0:  # Print status every 5 attempts
                        print(f"⏳ Status: {status} - Progress: {progress}%")
                    
                    # Check if task completed
                    if status in ['succeeded', 'completed']:
                        # Get the result
                        result_response = requests.get(
                            f"{SERVER_URL}/voice-clone/result/{task_id}",
                            headers=get_auth_headers(),
                            timeout=30
                        )
                        
                        if result_response.status_code == 200:
                            output_file = os.path.join(OUTPUT_DIR, "base_tts_output.wav")
                            with open(output_file, 'wb') as f:
                                f.write(result_response.content)
                            
                            print(f"✅ Base TTS successful!")
                            print(f"🔊 Output saved to: {output_file}")
                            
                            if 'X-Credits-Consumed' in result_response.headers:
                                print(f"💳 Credits consumed: {result_response.headers['X-Credits-Consumed']}")
                            
                            return True
                    
                    elif status in ['failed', 'cancelled', 'timeout']:
                        error = task_info.get('error_message') or status_data.get('message', 'Unknown error')
                        print(f"❌ Task {status}: {error}")
                        return False
                    
                    # Still processing, wait and continue
                    time.sleep(3)
                else:
                    print(f"⚠️ Status check failed: {status_response.status_code}")
                    time.sleep(3)
            
            print(f"❌ Timeout waiting for result")
            return False
        elif response.status_code == 401:
            print(f"❌ Authentication failed: Invalid or missing token")
            return False
        else:
            print(f"❌ Base TTS failed: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_voice_conversion(input_audio_path, reference_speaker="demo_speaker0"):
    """
    Test voice conversion (change voice of existing audio).
    Note: v2 server doesn't have /change_voice endpoint - skipping this test.
    
    Args:
        input_audio_path (str): Path to input audio file
        reference_speaker (str): Reference speaker to convert to
    """
    
    print(f"🔄 Voice conversion test...")
    print(f"⚠️  Note: v2 server doesn't support /change_voice endpoint")
    print(f"⏭️  Skipping voice conversion test")
    return True

def test_server_status():
    """Test if the server is running and accessible."""
    print(f"🏥 Testing server status...")
    
    try:
        # Test without auth first (should get 401 or work if no auth required)
        response = requests.get(f"{SERVER_URL}/docs", timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Server is running and accessible")
            print(f"📚 API docs available at: {SERVER_URL}/docs")
            return True
        else:
            print(f"⚠️  Server responded with status: {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        print(f"❌ Cannot connect to server at {SERVER_URL}")
        print(f"🔧 Make sure the F5-TTS server is running")
        return False
    except Exception as e:
        print(f"❌ Error testing server status: {e}")
        return False

def main():
    """Main test function."""
    
    # Allow token to be passed as environment variable
    global AUTH_TOKEN
    if 'F5_TTS_AUTH_TOKEN' in os.environ:
        AUTH_TOKEN = os.environ['F5_TTS_AUTH_TOKEN']
        print(f"🔐 Using token from environment variable")
    
    ensure_output_dir()
    
    # Test text - Chinese with numbers to test number conversion
    test_text = "你好，这是F5语音克隆系统的测试。我今天买了3个苹果和5个橙子，一共花了12.50元。我的电话号码是10086，今天是2024年11月21日。这个系统可以处理从1到9999的各种数字。"
    
    # English test text for comparison
    test_text_en = "Hello, this is a test of the F5-TTS voice cloning system with authentication. The quality of the synthesized speech should match the reference audio."
    
    print("🚀 Starting F5-TTS Voice Cloning Tests with Authentication")
    print(f"🔐 Authentication Token: {AUTH_TOKEN}")
    print(f"📝 Test Text (Chinese with numbers): {test_text}")
    print("=" * 70)
    
    # Test 0: Server status
    print("\n📋 Test 0: Server Status Check")
    if not test_server_status():
        print("❌ Server is not accessible. Stopping tests.")
        return
    
    # Test 1: Base TTS (default voice) - Chinese test
    print("\n📋 Test 1: Base TTS with default voice (Chinese with numbers)")
    test_base_tts(test_text)
    
    # Test 2: Existing voice (demo_speaker0) - Chinese test
    print("\n📋 Test 2: Speech synthesis with existing demo voice (Chinese with numbers)")
    test_with_existing_voice(test_text, "demo_speaker0")
    
    # Test 2b: English test for comparison
    print("\n📋 Test 2b: Speech synthesis with existing demo voice (English)")
    test_with_existing_voice(test_text_en, "demo_speaker0")
    
    # Test 3: Voice cloning with custom audio (if provided)
    if len(sys.argv) > 1:
        reference_audio = sys.argv[1]
        voice_name = "custom_voice_test"
        
        print(f"\n📋 Test 3: Voice cloning with custom audio")
        test_voice_cloning(reference_audio, test_text, voice_name)
    else:
        print(f"\n📋 Test 3: Voice cloning with existing demo audio")
        # Use the demo_speaker0.wav as reference for cloning test
        demo_audio = "/workspace/resources/demo_speaker0.wav"
        if os.path.exists(demo_audio):
            test_voice_cloning(demo_audio, test_text, "cloned_demo")
        else:
            print("⚠️  No custom audio provided and demo audio not found.")
            print("   Usage: python test_voice_cloning.py [path_to_your_audio.wav]")
            print("   Or set F5_TTS_AUTH_TOKEN environment variable")
    
    print("\n" + "=" * 70)
    print("🏁 Testing complete! Check the 'test_outputs' directory for results.")
    print("\n💡 Tips:")
    print("   - This test is designed for f5tts_serverv2.py (port 8000)")
    print("   - Chinese text with numbers tests automatic number conversion")
    print("   - Numbers are converted: 123→一百二十三, 12.50→十二点五零, 10086→一零零八六")
    print("   - Set F5_TTS_AUTH_TOKEN environment variable for your token")
    print("   - Make sure the v2 server is running: python -m uvicorn f5tts_serverv2:app --host 0.0.0.0 --port 8000")
    print("   - Check server logs if authentication fails")
    print("   - Verify your token is valid in the server configuration")

if __name__ == "__main__":
    main()

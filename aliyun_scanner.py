#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描阿里云DashScope API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
支持多种模型测试和余额查询
现在使用 GITHUB_SESSION 进行搜索，以规避普通 API 速率限制
"""

import os
import re
import time
import threading
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
import json
import random
from urllib.parse import urlparse

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# 阿里云DashScope API 配置
ALIYUN_BASE_URL = "https://dashscope.aliyuncs.com"
ALIYUN_CHAT_URL = f"{ALIYUN_BASE_URL}/compatible-mode/v1/chat/completions"

# 支持的模型列表
ALIYUN_MODELS = [
    "codeqwen1.5-7b-chat",
    "deepseek-r1",
    "deepseek-r1-distill-llama-70b",
    "deepseek-r1-distill-llama-8b",
    "deepseek-r1-distill-qwen-1.5b",
    "deepseek-r1-distill-qwen-14b",
    "deepseek-r1-distill-qwen-32b",
    "deepseek-r1-distill-qwen-7b",
    "deepseek-v3",
    "deepseek-v3.1",
    "qvq-max-2025-05-15",
    "qvq-plus",
    "qvq-plus-2025-05-15",
    "qwen-1.8b-chat",
    "qwen-1.8b-longcontext-chat",
    "qwen-14b-chat",
    "qwen-72b-chat",
    "qwen-7b-chat",
    "qwen-coder-plus",
    "qwen-coder-plus-1106",
    "qwen-coder-plus-latest",
    "qwen-coder-turbo",
    "qwen-coder-turbo-0919",
    "qwen-coder-turbo-latest",
    "qwen-long",
    "qwen-math-plus",
    "qwen-math-plus-0919",
    "qwen-math-plus-latest",
    "qwen-math-turbo",
    "qwen-math-turbo-0919",
    "qwen-math-turbo-latest",
    "qwen-max",
    "qwen-max-0107",
    "qwen-max-0403",
    "qwen-max-0428",
    "qwen-max-0919",
    "qwen-max-1201",
    "qwen-max-latest",
    "qwen-max-longcontext",
    "qwen-mt-plus",
    "qwen-mt-turbo",
    "qwen-plus",
    "qwen-plus-0919",
    "qwen-plus-2025-07-14",
    "qwen-plus-2025-09-11",
    "qwen-plus-latest",
    "qwen-tts-2025-05-22",
    "qwen-turbo",
    "qwen-turbo-0919",
    "qwen-turbo-latest",
    "qwen-vl-max",
    "qwen-vl-max-2025-04-02",
    "qwen-vl-ocr",
    "qwen-vl-ocr-latest",
    "qwen-vl-plus",
    "qwen-vl-plus-2025-08-15",
    "qwen1.5-0.5b-chat",
    "qwen1.5-1.8b-chat",
    "qwen1.5-110b-chat",
    "qwen1.5-14b-chat",
    "qwen1.5-32b-chat",
    "qwen1.5-72b-chat",
    "qwen1.5-7b-chat",
    "qwen2-0.5b-instruct",
    "qwen2-1.5b-instruct",
    "qwen2-57b-a14b-instruct",
    "qwen2-72b-instruct",
    "qwen2-7b-instruct",
    "qwen2.5-0.5b-instruct",
    "qwen2.5-1.5b-instruct",
    "qwen2.5-14b-instruct",
    "qwen2.5-32b-instruct",
    "qwen2.5-3b-instruct",
    "qwen2.5-72b-instruct",
    "qwen2.5-7b-instruct",
    "qwen2.5-coder-0.5b-instruct",
    "qwen2.5-coder-14b-instruct",
    "qwen2.5-coder-32b-instruct",
    "qwen2.5-coder-3b-instruct",
    "qwen2.5-coder-7b-instruct",
    "qwen2.5-math-1.5b-instruct",
    "qwen2.5-math-72b-instruct",
    "qwen2.5-math-7b-instruct",
    "qwen3-0.6b",
    "qwen3-1.7b",
    "qwen3-14b",
    "qwen3-235b-a22b",
    "qwen3-30b-a3b",
    "qwen3-32b",
    "qwen3-4b",
    "qwen3-8b",
    "qwen3-coder-480b-a35b-instruct",
    "qwen3-coder-plus",
    "qwen3-coder-plus-2025-07-22",
    "qwen3-max-preview",
    "qwen3-next-80b-a3b-instruct",
    "qwen3-next-80b-a3b-thinking"
]

# 测试模型配置（只测试qwen-max）
TEST_MODELS = [
    "qwen-max"          # 只测试qwen-max模型
]

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1.5  # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 300  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
live_keys_queue = Queue()    # 存活密钥
processed_keys = set()       # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock() # 文件写入锁

# 实时保存文件名（全局变量）
live_keys_filename = None
simple_keys_filename = None

# --- 阿里云DashScope API 相关函数 ---

def test_model_access(api_key, model_name):
    """测试模型访问权限"""
    try:
        # 阿里云DashScope API使用Authorization Bearer头部认证
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
            "max_tokens": 1,
            "temperature": 0.1
        }
        
        response = requests.post(ALIYUN_CHAT_URL, 
                               headers=headers, 
                               json=payload, 
                               timeout=15)
        
        if response.status_code == 200:
            return {"success": True, "model": model_name, "response": response.json()}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text}", "model": model_name}
            
    except Exception as e:
        return {"success": False, "error": str(e), "model": model_name}

def test_api_key_manually(api_key):
    """手动测试单个API密钥（用于调试）"""
    print(f"\n🧪 手动测试API密钥: {api_key[:20]}...")
    
    # 测试不同的认证方式
    test_methods = [
        {"name": "X-DashScope-API-Key", "headers": {"X-DashScope-API-Key": api_key, "Content-Type": "application/json"}},
        {"name": "Authorization Bearer", "headers": {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}},
        {"name": "Authorization", "headers": {"Authorization": api_key, "Content-Type": "application/json"}}
    ]
    
    payload = {
        "model": "qwen-turbo",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 1,
        "temperature": 0.1
    }
    
    for method in test_methods:
        print(f"   测试认证方式: {method['name']}")
        try:
            response = requests.post(ALIYUN_CHAT_URL, 
                                   headers=method['headers'], 
                                   json=payload, 
                                   timeout=10)
            
            print(f"   状态码: {response.status_code}")
            if response.status_code == 200:
                print(f"   ✅ 成功! 响应: {response.json()}")
                return True
            else:
                print(f"   ❌ 失败: {response.text[:200]}")
        except Exception as e:
            print(f"   ❌ 异常: {str(e)}")
    
    return False

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'aliyun_live_keys_{timestamp}.txt'
            simple_keys_filename = f'aliyun_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== 阿里云DashScope API密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"测试模型列表: {', '.join(TEST_MODELS)}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== 阿里云DashScope API密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        # 追加详细信息到详细文件
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   可访问模型数: {key_info['accessible_count']}/{key_info['total_test_models']}\n")
            f.write(f"   可访问模型: {', '.join(key_info['accessible_models'])}\n")
            
            if key_info['failed_models']:
                f.write(f"   失败模型:\n")
                for failed in key_info['failed_models']:
                    f.write(f"     - {failed['model']}: {failed['error'][:100]}\n")
            
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   文件: {key_info['file_path']}\n")
            f.write(f"   链接: {key_info['url']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write("-" * 60 + "\n")
        
        # 追加密钥到简化文件
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
        
        print(f"💾 已实时保存密钥到: {live_keys_filename}")

def check_aliyun_key(key):
    """验证阿里云DashScope API密钥是否有效"""
    if not key.startswith("sk-"):
        return {"status": "无效格式", "type": "invalid", "accessible_models": []}
    
    accessible_models = []
    failed_models = []
    
    # 测试几个代表性模型
    for model in TEST_MODELS:
        result = test_model_access(key, model)
        if result["success"]:
            accessible_models.append(model)
            print(f"   ✅ 模型 {model} 可访问")
        else:
            failed_models.append({"model": model, "error": result["error"]})
            print(f"   ❌ 模型 {model} 不可访问: {result['error'][:100]}")
        
        # 添加延迟避免API限制
        time.sleep(0.5)
    
    if accessible_models:
        return {
            "status": f"🟢 密钥存活 (Live) - 可访问 {len(accessible_models)}/{len(TEST_MODELS)} 个测试模型",
            "type": "live",
            "accessible_models": accessible_models,
            "failed_models": failed_models
        }
    else:
        return {
            "status": f"🔴 密钥无效/已吊销 (Invalid/Revoked) - 所有测试模型均不可访问",
            "type": "invalid",
            "accessible_models": [],
            "failed_models": failed_models
        }

# --- Github Session Search 相关函数 ---

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
                
            # 提取链接 (兼容 Session Search 返回的 HTML)
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

def process_content_file(file_url, worker_id):
    """下载并处理单个文件内容，提取并验证API密钥"""
    try:
        print(f"[Worker-{worker_id}] 正在检查: {file_url}")
        
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        file_content = resp.text
        
        # 使用正则表达式提取sk-密钥（支持更长的密钥格式，至少32位）
        pattern = r'(sk-[A-Za-z0-9]{32,})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            print(f"[Worker-{worker_id}] 🔍 测试密钥: {key[:20]}...")
            
            # 验证密钥
            result = check_aliyun_key(key)
            print(f"[Worker-{worker_id}] 🔑 密钥状态: {result['status']}")
            
            # 如果密钥存活，立即保存并添加到队列
            if result["type"] == "live":
                # 从URL中提取 Repo Name
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"
                
                key_info = {
                    'type': 'Aliyun DashScope Key',
                    'key': key,
                    'status': result['status'],
                    'accessible_models': result['accessible_models'],
                    'failed_models': result['failed_models'],
                    'total_test_models': len(TEST_MODELS),
                    'accessible_count': len(result['accessible_models']),
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                
                # 实时保存到文件
                save_live_key_immediately(key_info)
                
                # 同时添加到队列（用于最终统计）
                live_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已实时保存并记录存活密钥")
            
            # 添加延迟以避免API速率限制
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 处理文件时出错: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证阿里云DashScope密钥"""
    print(f"🚀 开始并发搜索阿里云DashScope密钥 (并发数: {MAX_WORKERS})")
    print("=" * 60)
    
    # 定义搜索查询（使用 GITHUB_SESSION 正则匹配格式）
    # 格式: `搜索关键词 /正则/`
    search_queries = [
        # 1. dashscope.aliyuncs.com 配合 sk- 密钥正则
        r'dashscope.aliyuncs.com /sk-[A-Za-z0-9]{32,}/',
        
        # 2. 阿里云 配合 sk- 密钥正则
        r'"阿里云" /sk-[A-Za-z0-9]{32,}/',
        
        # 3. "dashscope" 配合 sk- 密钥正则
        r'dashscope /sk-[A-Za-z0-9]{32,}/',

        # 4. 备用：仅搜索 dashscope.aliyuncs.com (防止正则搜索无结果)
        'dashscope.aliyuncs.com sk-',
    ]
    
    all_file_urls = set()
    
    # 收集所有搜索结果
    for query in search_queries:
        print(f"📡 执行搜索查询: {query}")
        urls = search_github(query, max_pages=5)
        all_file_urls.update(urls)
        # 避免太过频繁
        time.sleep(2)
    
    file_list = list(all_file_urls)
    print(f"🗂️ 去重后共 {len(file_list)} 个唯一文件需要处理")
    
    # 使用线程池并发处理文件
    if file_list:
        print(f"⚡ 启动 {MAX_WORKERS} 个工作线程进行并发处理...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            futures = [executor.submit(process_content_file, url, i % MAX_WORKERS + 1) for i, url in enumerate(file_list)]
            
            # 等待所有任务完成
            completed = 0
            total = len(file_list)
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    future.result()
                    print(f"📊 进度: {completed}/{total} ({(completed/total)*100:.1f}%)")
                except Exception as e:
                    print(f"❌ 任务异常: {e}")
    
    print("\n✅ 所有文件处理完成")

def save_results():
    """保存扫描结果到文件（仅在没有实时保存时使用）"""
    global live_keys_filename, simple_keys_filename
    
    live_keys = []
    
    # 从队列中获取所有结果
    while not live_keys_queue.empty():
        live_keys.append(live_keys_queue.get())
    
    # 如果已经有实时保存的文件，只需要更新统计信息
    if live_keys_filename is not None and live_keys:
        with file_lock:
            # 在实时保存文件末尾添加扫描完成统计
            with open(live_keys_filename, 'a', encoding='utf-8') as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write("=== 扫描完成统计 ===\n")
                f.write(f"扫描结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总计发现存活密钥数量: {len(live_keys)}\n")
                f.write(f"处理的唯一密钥总数: {len(processed_keys)}\n")
                f.write("=" * 60 + "\n")
            
            # 在简化文件末尾添加统计
            with open(simple_keys_filename, 'a', encoding='utf-8') as f:
                f.write(f"\n# 扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# 总计: {len(live_keys)} 个存活密钥\n")
        
        print(f"📊 已更新实时保存文件的扫描统计: {live_keys_filename}")
        print(f"📊 总计发现 {len(live_keys)} 个存活密钥")
        
    # 如果没有实时保存（即没有发现存活密钥），创建传统的结果文件
    elif not live_keys_filename and live_keys:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'aliyun_live_keys_{timestamp}.txt'
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("=== 阿里云DashScope API密钥扫描结果 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"发现存活密钥数量: {len(live_keys)}\n")
            f.write(f"测试模型列表: {', '.join(TEST_MODELS)}\n")
            f.write("=" * 60 + "\n\n")
            
            for i, key_info in enumerate(live_keys, 1):
                f.write(f"{i}. {key_info['type']}\n")
                f.write(f"   密钥: {key_info['key']}\n")
                f.write(f"   状态: {key_info['status']}\n")
                f.write(f"   可访问模型数: {key_info['accessible_count']}/{key_info['total_test_models']}\n")
                f.write(f"   可访问模型: {', '.join(key_info['accessible_models'])}\n")
                
                if key_info['failed_models']:
                    f.write(f"   失败模型:\n")
                    for failed in key_info['failed_models']:
                        f.write(f"     - {failed['model']}: {failed['error'][:100]}\n")
                
                f.write(f"   仓库: {key_info['repo']}\n")
                f.write(f"   文件: {key_info['file_path']}\n")
                f.write(f"   链接: {key_info['url']}\n")
                f.write(f"   发现时间: {key_info['discovered_time']}\n")
                f.write("-" * 60 + "\n")
        
        print(f"💾 已将 {len(live_keys)} 个存活密钥保存到: {filename}")
        
        # 保存简化版本（仅密钥）
        simple_filename = f'aliyun_keys_simple_{timestamp}.txt'
        with open(simple_filename, 'w', encoding='utf-8') as f:
            f.write("=== 阿里云DashScope API密钥列表 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总计: {len(live_keys)} 个存活密钥\n")
            f.write("=" * 40 + "\n\n")
            
            for key_info in live_keys:
                f.write(f"{key_info['key']}\n")
        
        print(f"📝 已将密钥列表保存到: {simple_filename}")
    
    # 如果没有发现任何存活密钥
    if not live_keys:
        print("\n📝 未发现存活的阿里云DashScope密钥")

def main():
    """主函数"""
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION 以使用 Session 搜索模式")
        return

    print("🔍 阿里云DashScope API密钥并发扫描器 (Session Search Mode)")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 支持模型总数: {len(ALIYUN_MODELS)}")
    print(f"   - 测试模型: {', '.join(TEST_MODELS)}")
    print("=" * 50)
    
    start_time = time.time()
    
    try:
        print("\n🚀 开始扫描...")
        
        # 执行并发搜索和验证
        concurrent_search_and_validate()
        
        # 保存结果
        save_results()
        
        end_time = time.time()
        elapsed_time = end_time - start_time
        
        print(f"\n🏁 扫描完成!")
        print(f"⏱️ 总耗时: {elapsed_time:.2f}秒")
        print(f"🔧 处理的唯一密钥数: {len(processed_keys)}")
        print(f"✅ 发现存活密钥数: {live_keys_queue.qsize()}")
        
    except KeyboardInterrupt:
        print("\n🛑 用户中断扫描")
        save_results()  # 保存已找到的结果
    except Exception as e:
        print(f"\n❌ 扫描过程中发生错误: {e}")
        save_results()  # 保存已找到的结果

if __name__ == '__main__':
    # 检查是否有命令行参数用于测试
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # 测试模式：使用提供的示例密钥进行测试
        test_keys = [
            "sk-579198d69b204050925290d4782091ea",
            "sk-6fd081564985497f886dbbf7d35feba8"
        ]
        
        print("🧪 测试模式：验证API调用格式")
        print("=" * 50)
        
        for key in test_keys:
            success = test_api_key_manually(key)
            if success:
                print(f"✅ 密钥 {key[:20]}... 测试成功")
                break
            else:
                print(f"❌ 密钥 {key[:20]}... 测试失败")
        
        print("\n测试完成。如果所有密钥都失败，可能需要检查API格式或密钥已过期。")
    else:
        main()

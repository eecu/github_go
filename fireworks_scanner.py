#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描fw_开头的Fireworks AI API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
"""

import os
import re
import time
import threading
import random
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
import urllib3

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
# 从环境变量中获取GitHub Session Cookie
GITHUB_SESSION = os.getenv('GITHUB_SESSION')
if not GITHUB_SESSION:
    print("⚠️ 警告: 未设置 GITHUB_SESSION 环境变量，无法使用网页搜索模式。")
    print("   请在 .env 文件中配置 GITHUB_SESSION=your_cookie_here")
    # 为了兼容性，如果不设置Session，可以抛出错误或者尝试其他方式，这里强制要求Session
    raise ValueError("请设置 GITHUB_SESSION 环境变量 (GitHub user_session Cookie)")

# Fireworks AI API配置
FIREWORKS_API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODELS = [
    "accounts/fireworks/models/deepseek-r1",
    "accounts/fireworks/models/deepseek-r1-0528",
    "accounts/fireworks/models/deepseek-r1-basic",
    "accounts/fireworks/models/deepseek-v3",
    "accounts/fireworks/models/deepseek-v3-0324",
    "accounts/fireworks/models/deepseek-v3p1",
    "accounts/fireworks/models/flux-1-dev-fp8",
    "accounts/fireworks/models/flux-1-schnell-fp8",
    "accounts/fireworks/models/flux-kontext-max",
    "accounts/fireworks/models/flux-kontext-pro",
    "accounts/fireworks/models/glm-4p5",
    "accounts/fireworks/models/glm-4p5-air",
    "accounts/fireworks/models/gpt-oss-120b",
    "accounts/fireworks/models/gpt-oss-20b",
    "accounts/fireworks/models/kimi-k2-instruct",
    "accounts/fireworks/models/kimi-k2-instruct-0905",
    "accounts/fireworks/models/llama-v3p1-405b-instruct",
    "accounts/fireworks/models/llama-v3p1-70b-instruct",
    "accounts/fireworks/models/llama-v3p1-8b-instruct",
    "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "accounts/fireworks/models/llama4-maverick-instruct-basic",
    "accounts/fireworks/models/llama4-scout-instruct-basic",
    "accounts/fireworks/models/mixtral-8x22b-instruct",
    "accounts/fireworks/models/qwen2p5-vl-32b-instruct",
    "accounts/fireworks/models/qwen3-235b-a22b",
    "accounts/fireworks/models/qwen3-235b-a22b-instruct-2507",
    "accounts/fireworks/models/qwen3-235b-a22b-thinking-2507",
    "accounts/fireworks/models/qwen3-30b-a3b",
    "accounts/fireworks/models/qwen3-30b-a3b-instruct-2507",
    "accounts/fireworks/models/qwen3-30b-a3b-thinking-2507",
    "accounts/fireworks/models/qwen3-coder-30b-a3b-instruct",
    "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
    "accounts/fireworks/models/qwen3-embedding-8b",
    "accounts/justin-mainfunc-0d2c74/models/genspark-kimi-lora-09-10-48-0525",
    "accounts/perplexity/models/r1-1776",
    "accounts/scale-ai/models/arctic-text2sql-r1-7b-public",
    "accounts/sentientfoundation-serverless/models/dobby-mini-unhinged-plus-llama-3-1-8b",
    "accounts/sentientfoundation/models/dobby-unhinged-llama-3-3-70b-new",
    "accounts/tvergho-87e44d/models/debatecards-70b-ft-3epoch-dpo-v2"
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

# --- 密钥验证函数 ---

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'fireworks_live_keys_{timestamp}.txt'
            simple_keys_filename = f'fireworks_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Fireworks AI API密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"API端点: {FIREWORKS_API_URL}\n")
                f.write(f"测试模型: accounts/fireworks/models/llama-v3p1-8b-instruct\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Fireworks AI API密钥列表 (实时更新) ===\n")
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

def check_fireworks_key(key):
    """验证Fireworks AI API密钥是否有效"""
    if not key.startswith("fw_"):
        return "无效格式"
    
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        
        # 使用一个轻量级的模型进行测试
        test_model = "accounts/fireworks/models/llama-v3p1-8b-instruct"  # 选择一个较小的模型
        
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
            FIREWORKS_API_URL,
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

def get_headers():
    """构造 GitHub 搜索请求头"""
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    }

def process_file_url(file_url, worker_id):
    """处理单个文件URL，下载内容并提取API密钥"""
    try:
        # 构造Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            # 将HTML URL转换为Raw URL
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        # print(f"[Worker-{worker_id}] 正在检查: {raw_url}")
        
        try:
            resp = requests.get(raw_url, timeout=15)
            if resp.status_code != 200:
                # print(f"[Worker-{worker_id}] ❌ 下载失败: {resp.status_code}")
                return
            
            file_content = resp.text
        except Exception as e:
            print(f"[Worker-{worker_id}] ❌ 下载出错: {e}")
            return
        
        # 使用正则表达式提取fw_密钥（Fireworks格式）
        pattern = r'(fw_[A-Za-z0-9_-]{20,})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            status = check_fireworks_key(key)
            print(f"[Worker-{worker_id}] 🔑 发现密钥: {key[:20]}... | 状态: {status}")
            
            # 如果密钥存活，立即保存并添加到队列（只保存完全存活的密钥）
            if "🟢 存活" in status:
                # 从URL中解析仓库信息
                repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
                repo_name = repo_match.group(1) if repo_match else "unknown/repo"
                
                key_info = {
                    'type': 'Fireworks AI Key',
                    'key': key,
                    'status': status,
                    'url': file_url, # 使用原始HTML链接方便访问
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
        # 这里使用 params 让 requests 自动处理 URL 编码
        # 注意：GitHub 搜索的正则需要 /pattern/ 格式，且通常不需要引号包裹
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
                # 保存调试页面
                with open("debug_github_error.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                print("   💾 已保存错误页面到 debug_github_error.html 供分析")
                if "Sign in" in resp.text: print("   (检测到登录跳转)")
                break
                
            # 提取链接，使用更宽松的正则以匹配可能的格式
            # 匹配 href="/user/repo/blob/..." 兼容 harvester-main 的逻辑
            # 注意：有些结果可能在 JSON 数据中 (react app)，但通常 SSR 会返回 HTML
            
            # 方法 1: 尝试匹配 HTML 中的链接
            links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            # 方法 2: 如果 HTML 是空的或者动态加载，尝试寻找 json payload (GitHub 经常变)
            # 但 session search 通常返回 SSR HTML.
            
            if not links:
                # 尝试 harvester-main 的 regex (带/不带引号)
                links_v2 = re.findall(r'href="(/[^\s"]+/blob/(?:[^"]+)?)#L\d+"', resp.text)
                if links_v2:
                    links.extend(links_v2)
            
            if not links:
                if page == 1:
                    print(f"   ⚠️ Page 1 无结果。")
                    # print(f"   (Debug: 响应长度 {len(resp.text)})")
                break
            
            unique_links_page = set()
            for link in links:
                # 只要第一部分 URL，去掉 #L... (如果有)
                clean_link = link.split('#')[0]
                if clean_link.endswith(".md") or clean_link.endswith(".txt"):
                    continue # 可选：跳过文档
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
    """并发搜索和验证fw_密钥"""
    print(f"🚀 开始并发搜索Fireworks AI fw_密钥 (并发数: {MAX_WORKERS})")
    print(f"ℹ️ 使用 GitHub Session 网页搜索模式")
    print("=" * 60)
    
    # 定义搜索查询
    # 注意：
    # 1. 普通关键词建议用引号包裹，如 '"fw_"'
    # 2. 正则表达式必须用斜杠包裹且不能有引号，如 '/fw_[a-zA-Z0-9]{20}/'
    # 3. 如果搜索结果为0，尝试去掉外层引号
    search_queries = [
        '/fw_[1-9A-HJ-NP-Za-km-z]{24}/', # 用户自定义正则
        '"fw_"', #备用：普通搜索
    ]
    
    all_file_urls = set()
    
    # 收集所有搜索结果
    for query in search_queries:
        urls = search_github_session(query, max_pages=5) # 每个查询搜5页
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
            f.write(f"\n=== Fireworks AI密钥扫描结果 (实时保存) ===\n")
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
        print("\n📝 未发现存活的Fireworks AI密钥")

def main():
    """主函数"""
    print("🔍 Fireworks AI API密钥并发扫描器")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - API端点: {FIREWORKS_API_URL}")
    print(f"   - 支持模型数量: {len(FIREWORKS_MODELS)}")
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

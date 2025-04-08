# -*- coding: utf-8 -*-
# @Author: FWill
# @Time: 2025-04-08 19:00
# @File: AI web chat.py
# @Description: This code uses the flask library to implement a multifunctional AI conversation on a web page.

"""
Flask服务端程序，集成ollama大模型、知识库查询、函数执行、文件上传等功能
支持流式响应、对话历史记录、生成中断等特性
"""

# 导入必要库
from flask import Flask, request, jsonify, Response, render_template  # Web框架相关
import ollama  # 大模型客户端库
import os, re, time, datetime, codecs, logging  # 系统/工具库
from threading import Lock  # 线程锁
import subprocess  # 子进程管理
from datetime import datetime

# 全局配置
generation_stop_flag = False  # 生成中断标志位（线程安全）
generation_lock = Lock()  # 用于保护共享资源的线程锁


# 自定义UTF-8编码日志处理器
class UTF8Handler(logging.FileHandler):
    """强制使用UTF-8编码写入日志文件，解决中文乱码问题"""

    def __init__(self, filename, mode='a', encoding=None, delay=False):
        super().__init__(filename, mode, encoding=encoding, delay=delay)
        self.stream = codecs.open(filename, mode, encoding='utf-8')  # 重写文件流编码


# 初始化Flask应用
app = Flask(__name__, template_folder='.')  # 设置模板目录为当前目录


# 工具函数：获取本机IPv6地址
def get_ipv6_address():
    """通过执行系统命令获取IPv6地址"""
    output = os.popen("ipconfig /all").read()  # Windows系统命令
    result = re.findall(r"(([a-f0-9]{1,4}:){7}[a-f0-9]{1,4})", output, re.I)
    return result[0][0] if result else None  # 返回第一个匹配结果或None


# 工具函数：生成格式化时间字符串
def get_time(fmt: str = '%Y年%m月%d日_%H时%M分%S秒') -> str:
    """生成指定格式的时间字符串"""
    ts = time.time()  # 获取时间戳
    ta = time.localtime(ts)  # 转换为本地时间元组
    return time.strftime(fmt, ta)  # 格式化为字符串


# 配置日志系统
logging.basicConfig(
    level=logging.INFO,  # 设置最低日志级别
    format='%(asctime)s - %(levelname)s - %(message)s',  # 日志格式
    handlers=[  # 多处理器配置
        UTF8Handler('chat.log'),  # 写入chat.log文件
        logging.StreamHandler()  # 同时输出到控制台
    ]
)


# 核心AI交互函数
def chat_ollama(user_message, stream):
    """与ollama服务进行交互"""
    host = 'http://localhost:11434'  # ollama默认服务地址
    cli = ollama.Client(host=host)  # 创建客户端实例

    # 发送聊天请求（启用流式响应）
    response = cli.chat(
        model=modname,  # 使用全局配置的模型名称
        messages=[{'role': 'user', 'content': user_message}],  # 用户消息列表
        stream=stream  # 启用流式模式（逐步返回生成内容）
    )
    return response


# 对话记录存储相关函数
def save_chat_record(user_message, ai_response):
    """保存完整对话记录到文件"""
    os.makedirs('chatlist', exist_ok=True)  # 确保存储目录存在
    date_str = datetime.now().strftime("%Y%m%d")  # 生成日期字符串
    filename = os.path.join('chatlist', f"{date_str}.txt")  # 构建文件路径

    # 清理AI响应中的思考标记（保留正式回复）
    cleaned_response = re.sub(
        r'###正在思考###.*?###总结部分###',  # 非贪婪匹配思考部分
        '',
        ai_response,
        flags=re.DOTALL  # 使.匹配换行符
    ).strip()

    # 追加写入文件（UTF-8编码）
    with codecs.open(filename, 'a', encoding='utf-8') as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = f"[{timestamp}] 用户的问题: {user_message}\nAI回复: {cleaned_response}###RECORD_SEPARATOR###\n"
        f.write(record)


def get_chat_records(date_str, num_records=5):
    """获取指定日期的最近N条对话记录"""
    filename = os.path.join('chatlist', f"{date_str}.txt")

    if not os.path.exists(filename):
        app.logger.warning(f"历史记录文件 {filename} 不存在")
        return []

    try:
        with codecs.open(filename, 'r', encoding='utf-8') as f:
            lines = f.read()

        # 使用正则表达式匹配完整对话记录
        pattern = r'\[.*?\]\s*用户的问题:[\s\S]*?AI回复:[\s\S]*?(?=###RECORD_SEPARATOR###|\Z)'
        records = re.findall(pattern, lines, re.DOTALL)

        if records:
            processed_records = []
            # 对返回记录进行长度截断处理
            for record in records[-num_records:]:
                if len(record) > max_history_length:
                    trimmed_record = record[:max_history_length] + '...'
                    app.logger.debug(f"截断历史记录: 原长度{len(record)} → 新长度{len(trimmed_record)}")
                    processed_records.append(trimmed_record)
                else:
                    processed_records.append(record)
            return processed_records
        else:
            app.logger.warning("未找到匹配的历史记录")
            return []
    except Exception as e:
        app.logger.error(f"读取历史记录失败: {str(e)}")
        return []


# 知识库查询函数
def find_best_matches(user_query):
    """从知识库目录查找最相关的文件内容"""
    folder_path = 'listku/processed_listku'
    if not os.path.exists(folder_path):
        app.logger.warning("知识库文件夹不存在")
        return []

    files = os.listdir(folder_path)
    if not files:
        app.logger.warning("知识库文件夹为空")
        return []

    matches = []
    query_chars = set(user_query.lower())  # 将查询词转为小写集合

    for filename in files:
        if not filename.endswith('.txt'):
            continue

        base_filename = os.path.splitext(filename)[0].lower()
        score = 0

        # 计算字符匹配得分（每个字符出现次数*2）
        for char in query_chars:
            score += base_filename.count(char) * 2

        # 计算单词匹配得分（每个单词长度*3）
        for word in user_query.lower().split():
            if word in base_filename:
                score += len(word) * 3

        if score > threshold:  # 超过阈值则记录
            try:
                with codecs.open(os.path.join(folder_path, filename), 'r', encoding='utf-8') as f:
                    content = re.sub(r'\s+', ' ', f.read().strip())  # 合并多余空白
                    content = content.replace('\n', ' ')  # 移除换行符
                    if not content.strip():
                        continue
                    matches.append((filename, content, score))
            except Exception as e:
                app.logger.error(f"读取知识库文件失败: {str(e)}")

    matches.sort(key=lambda x: x[2], reverse=True)  # 按得分降序排列
    return matches[:max_results]  # 返回前N个结果


# API路由：获取可用函数列表
@app.route('/api/list_funcs', methods=['GET'])
def list_funcs():
    """返回func目录下可用的Python函数列表"""
    try:
        func_dir = 'func'
        actual_files = [f for f in os.listdir(func_dir) if
                        f.endswith('.py') and os.path.isfile(os.path.join(func_dir, f))]
        app.logger.info(f"检测到函数目录文件: {actual_files}")
        return {'funcs': actual_files}
    except Exception as e:
        app.logger.error(f"获取函数列表异常: {str(e)}")
        return {'funcs': []}, 500


# API路由：执行指定函数
@app.route('/api/run_func', methods=['GET'])
def run_func():
    """执行func目录下的指定Python函数"""
    func_name = request.args.get('func')
    raw_input = request.args.get('raw_input', '')

    if not func_name or not func_name.endswith('.py'):
        return "无效的函数请求", 400

    func_path = os.path.join('func', func_name)
    if not os.path.exists(func_path):
        return f"函数文件不存在: {func_path}", 404

    try:
        # 执行子进程并捕获输出
        result = subprocess.run(
            ['python', '-u', func_path, raw_input],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            timeout=60
        )
        output = result.stdout.strip()
        error = result.stderr.strip()

        # 对输出结果进行长度截断处理
        max_func_length = request.args.get('max_func_length', 150, type=int)
        if len(output) > max_func_length:
            output = output[:max_func_length] + '...'
            app.logger.debug(f"函数返回截断: 原长度{len(result.stdout)} → 新长度{len(output)}")

        if error:
            app.logger.error(f"子进程错误: {error}")
            return f"执行错误: {error}", 500
        return output
    except Exception as e:
        return f"执行错误: {str(e)}", 500


# 文件上传路由
@app.route('/api/upload', methods=['POST'])
def upload_image():
    """处理图片上传请求"""
    if 'image' not in request.files:
        return {'error': '未选择文件'}, 400

    file = request.files['image']
    if file.filename == '':
        return {'error': '未选择文件'}, 400

    if not file.filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        return {'error': '仅支持PNG/JPG格式'}, 400

    upload_folder = 'image'
    os.makedirs(upload_folder, exist_ok=True)

    # 生成唯一文件名（避免覆盖）
    timestamp = get_time()
    original_extension = os.path.splitext(file.filename)[1]
    filename_base = f"{timestamp}{original_extension}"
    counter = 1
    while os.path.exists(os.path.join(upload_folder, filename_base)):
        filename_base = f"{timestamp}_{counter}{original_extension}"
        counter += 1

    save_path = os.path.join(upload_folder, filename_base)

    try:
        file.save(save_path)
        app.logger.info(f"图片上传成功: {filename_base}")
        return {'filename': filename_base}
    except Exception as e:
        app.logger.error(f"图片保存失败: {str(e)}")
        return {'error': '文件保存失败'}, 500


# 主页路由
@app.route('/')
def index():
    """返回主页面HTML模板"""
    return render_template('index.html', ipv6_address=get_ipv6_address())


# 生成中断路由
@app.route('/api/stop_generation', methods=['POST'])
def stop_generation():
    """设置生成中断标志位"""
    global generation_stop_flag
    with generation_lock:
        generation_stop_flag = True
    return jsonify({'status': 'stopping'}), 200


# 核心对话路由
@app.route('/api/chat', methods=['POST'])
def chat():
    """处理用户对话请求"""
    use_memory = request.json.get('useMemory', False)  # 是否使用对话记忆
    use_database = request.json.get('useDatabase', False)  # 是否查询知识库
    user_message = request.json['message']  # 用户原始消息

    settings = request.json.get('settings', {})

    # 从请求中提取配置参数
    global re_chatlist, max_history_length, max_results, re_max_listku, modname, max_func_length
    re_chatlist = settings.get('re_chatlist', 2)  # 历史记录返回数量
    max_history_length = settings.get('max_history_length', 200)  # 单条历史最大长度
    max_results = settings.get('max_results', 2)  # 知识库返回数量
    re_max_listku = settings.get('re_max_listku', 150)  # 知识库内容截断长度
    max_func_length = settings.get('max_func_length', 150)  # 函数返回截断长度
    modname = settings.get('modname', 'deepseek-r1:8b')  # AI模型名称

    # 构建历史记录上下文
    history_parts = []
    if use_memory:
        today_str = datetime.now().strftime("%Y%m%d")
        matched_records = get_chat_records(today_str, re_chatlist)
        for i, record in enumerate(matched_records, start=1):
            history_parts.append(f"[历史对话 {i}]:\n{record}")

    if use_database:
        matched_files = find_best_matches(user_message)
        matched_files = matched_files[:max_results]
        for i, (filename, content, match_ratio) in enumerate(matched_files, start=1):
            trimmed_content = content[:re_max_listku]
            if len(content) > re_max_listku:
                trimmed_content += '...'
                app.logger.debug(f"数据库内容截断: 原长度{len(content)} → 新长度{len(trimmed_content)}")
            if not trimmed_content.strip():
                continue
            history_parts.append(f"[数据库资料 {i} - {filename} (关联性: {match_ratio:.2f})]:\n{trimmed_content}")

    full_history = "\n\n".join(history_parts) if history_parts else ""

    # 添加函数执行结果（如果存在）
    if 'currentFunc' in request.json and request.json['currentFunc']:
        func_result = request.json['currentFunc']
        full_content = f"{user_message}\n\n[函数执行结果]:\n{func_result}\n\n{full_history}"
    else:
        full_content = f"{user_message}\n\n{full_history}" if full_history else user_message

    # 定义生成器函数（流式响应）
    def generate(content):
        global generation_stop_flag  # 声明使用全局变量
        try:
            app.logger.info(f"流式处理开始: {content[:50]}...")
            stream = chat_ollama(content, True)  # 获取流式响应
            full_response = ""

            # 发送历史记录信息
            yield "\n\n📌 正在参考以下信息：\n\n"
            for part in history_parts:
                yield f"{part.replace('###RECORD_SEPARATOR###', '')}\n\n"
            yield "💡 AI思考过程：\n"

            # 逐步处理流式响应
            for chunk in stream:
                with generation_lock:
                    if generation_stop_flag:
                        generation_stop_flag = False
                        raise GeneratorExit("用户请求停止生成")

                content = chunk['message']['content']
                # 处理思考标记
                if content.startswith('<think>'):
                    content = content.replace('<think>', '\n###正在思考###\n', 1)
                elif content.startswith('</think>'):
                    content = content.replace('</think>', '\n###总结部分###\n', 1)

                app.logger.debug(f"发送数据块: {content}")
                yield f"{content}"
                full_response += content

            app.logger.info("流式处理完成")
            # 保存完整对话记录（包含思考过程）
            save_chat_record(user_message, full_response.strip())
        except GeneratorExit as e:
            app.logger.warning(f"流式处理中止: {str(e)}")
        except Exception as e:
            app.logger.error(f"流式错误: {str(e)}")
            yield f"[ERROR] {str(e)}\n\n"

    # 返回流式响应
    return Response(generate(full_content), mimetype='text/event-stream')


# 程序入口
if __name__ == '__main__':
    threshold = 15  # 知识库匹配最低得分阈值

    # 获取IPv6地址并启动服务
    ipv6_address = get_ipv6_address()
    if ipv6_address:
        app.run(host=ipv6_address, port=91, debug=True, threaded=True)
    else:
        print("No valid IPv6 address found. Falling back to localhost.")
        app.run(host='localhost', port=91, debug=True, threaded=True)
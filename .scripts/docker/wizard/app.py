import http.server
import json
import subprocess
import threading
import os
import sys
import time
import socket
import urllib.parse

import socketserver

# 尝试引入多线程 HTTP 服务器以解决 Docker 挂起时阻塞整个 Web 服务的问题
class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

HTTPServer = ThreadingHTTPServer


# 端口配置
PORT = 8899

# 路径计算
WIZARD_DIR = os.path.dirname(os.path.abspath(__file__))
DOCKER_DIR = os.path.dirname(WIZARD_DIR)
PROJECT_ROOT = os.path.dirname(os.path.dirname(DOCKER_DIR))

# 全局状态变量
deploy_logs = []
deploy_in_progress = False
deploy_status = "idle"  # idle, running, success, failed
deploy_thread = None
deploy_stop_event = threading.Event()
last_ui_log_time = 0.0

# 默认要监控的中间件服务列表
ALL_MIDDLEWARES = [
    "Nacos", "PostgresSQL", "Redis", "TDengine", "MinIO", "Kafka",
    "SRS", "ZLMediaKit", "Milvus", "EMQX", "GPUStack", "NodeRED"
]

# 默认的核心微服务列表
ALL_SERVICES = ["DEVICE", "AI", "VIDEO", "WEB"]

def get_ip_address():
    """获取本机局域网IP地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def is_verbose_log(msg):
    """判断是否为冗余的进度日志"""
    msg_lower = msg.lower()
    if "error" in msg_lower or "failed" in msg_lower:
        return False
    
    verbose_keywords = [
        "downloading", "extracting", "waiting", "pulling fs layer",
        "already exists", "download complete", "pull complete",
        "verifying checksum", "downloaded from"
    ]
    return any(kw in msg_lower for kw in verbose_keywords)

def log_message(msg):
    """向全局日志列表中添加日志"""
    global last_ui_log_time
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    if not is_verbose_log(msg):
        deploy_logs.append(formatted_msg)
        last_ui_log_time = time.time()
    else:
        current_time = time.time()
        # 如果距离上一次向 UI 发送日志已经过了 15 秒以上，发送一条进度汇总日志，避免用户觉得卡住
        if current_time - last_ui_log_time > 15:
            clean_msg = msg.strip()
            hint_msg = f"[{timestamp}] [Deploy Wizard] 正在拉取 Docker 镜像中，当前进度: {clean_msg}"
            deploy_logs.append(hint_msg)
            last_ui_log_time = current_time
    print(formatted_msg)

def read_env_file(file_path):
    """解析.env文件为字典"""
    config = {}
    if not os.path.exists(file_path):
        return config
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except Exception as e:
        print(f"读取 .env 失败 {file_path}: {e}")
    return config

def update_env_file(file_path, updates):
    """增量/覆写更新.env文件"""
    if not os.path.exists(file_path):
        # 如果不存在，尝试从同一目录下的 env.example 或 .env.example 复制
        dir_name = os.path.dirname(file_path)
        example_paths = [
            file_path + ".example",
            os.path.join(dir_name, "env.example"),
            os.path.join(dir_name, ".env.example")
        ]
        example_content = ""
        for ep in example_paths:
            if os.path.exists(ep):
                try:
                    with open(ep, "r", encoding="utf-8") as f:
                        example_content = f.read()
                    break
                except Exception:
                    pass
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(example_content)
        except Exception as e:
            print(f"创建 .env 失败 {file_path}: {e}")
            return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"读取 .env 失败 {file_path}: {e}")
        return

    out_lines = []
    seen_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k in updates:
                out_lines.append(f"{k}={updates[k]}\n")
                seen_keys.add(k)
                continue
        out_lines.append(line)

    for k, v in updates.items():
        if k not in seen_keys:
            out_lines.append(f"{k}={v}\n")

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
    except Exception as e:
        print(f"写入 .env 失败 {file_path}: {e}")

def parse_compose_services():
    """解析docker-compose.yml中哪些服务处于启用状态"""
    compose_path = os.path.join(DOCKER_DIR, "docker-compose.yml")
    enabled = {}
    for m in ALL_MIDDLEWARES:
        enabled[m] = False

    if not os.path.exists(compose_path):
        return enabled

    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return enabled

    current_service = None
    in_services = False

    for line in lines:
        stripped = line.strip()
        if line.startswith("services:"):
            in_services = True
            continue

        if in_services:
            if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
                name = stripped[:-1].strip()
                current_service = name
                if name in enabled:
                    # 检查这一行是否被注释了
                    enabled[name] = not line.lstrip().startswith("#")
            elif line.startswith("volumes:") or line.startswith("networks:") or (stripped and not line.startswith(" ") and not stripped.startswith("#")):
                in_services = False
                current_service = None

    return enabled

def get_mirrored_image_line(line, mirror_prefix):
    """如果指定了镜像加速源前缀，重写 docker-compose 中的 image 字段"""
    stripped = line.strip()
    if not stripped.startswith("image:"):
        return line
    
    parts = stripped.split(":", 1)
    if len(parts) < 2:
        return line
        
    image_name = parts[1].strip().strip("'\"")
    
    # 如果已经带了其他镜像前缀，先去掉以支持切换
    first_slash = image_name.find("/")
    if first_slash != -1:
        domain_part = image_name[:first_slash]
        if "." in domain_part:
            image_name = image_name[first_slash+1:]

    # 判断是否为官方镜像（不含 "/"）
    if "/" not in image_name:
        image_name = "library/" + image_name
        
    # 提取镜像站点域名
    prefix = mirror_prefix.replace("https://", "").replace("http://", "").strip().strip("/")
    new_image = f"{prefix}/{image_name}"
    
    indent = line[:line.find("image:")]
    return f"{indent}image: {new_image}\n"

def toggle_compose_services(enabled_services, mirror_prefix=""):
    """修改docker-compose.yml，注释掉未启用的中间件，并按需替换镜像加速源"""
    compose_path = os.path.join(DOCKER_DIR, "docker-compose.yml")
    backup_path = compose_path + ".bak"

    if not os.path.exists(backup_path):
        try:
            with open(compose_path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            log_message(f"创建 docker-compose.yml.bak 备份失败: {e}")
            return False

    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        log_message(f"读取备份模板失败: {e}")
        return False

    out_lines = []
    current_service = None
    in_services = False

    for line in lines:
        stripped = line.strip()
        if line.startswith("services:"):
            in_services = True
            out_lines.append(line)
            continue

        if in_services:
            if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
                current_service = stripped[:-1].strip()
            elif line.startswith("volumes:") or line.startswith("networks:") or (stripped and not line.startswith(" ") and not stripped.startswith("#")):
                in_services = False
                current_service = None

        if current_service:
            # 判断服务或其初始化依赖是否被启用
            service_root = current_service
            if service_root.endswith("-init"):
                service_root = service_root[:-5]

            is_enabled = service_root in enabled_services
            # 特殊情况处理：SRS 和 ZLMediaKit 合并启用流媒体服务
            if service_root == "SRS" or service_root == "ZLMediaKit":
                is_enabled = "SRS" in enabled_services or "ZLMediaKit" in enabled_services

            # 如果未启用，则注释整行，确保不破坏原注释格式
            if not is_enabled:
                if stripped.startswith("#"):
                    out_lines.append(line)
                else:
                    out_lines.append("#" + line)
            else:
                # 启用时，若以“#”开头则移除首个“#”
                work_line = line[1:] if line.startswith("#") else line
                
                # 如果配置了镜像加速源前缀，重写镜像字段
                if mirror_prefix and "image:" in work_line:
                    is_still_commented = work_line.startswith("#")
                    sub_line = work_line[1:] if is_still_commented else work_line
                    rewritten = get_mirrored_image_line(sub_line, mirror_prefix)
                    work_line = "#" + rewritten if is_still_commented else rewritten
                    
                out_lines.append(work_line)
        else:
            out_lines.append(line)

    try:
        with open(compose_path, "w", encoding="utf-8") as f:
            f.writelines(out_lines)
        return True
    except Exception as e:
        log_message(f"写入 docker-compose.yml 失败: {e}")
        return False

def detect_current_mirror():
    """检测 docker-compose.yml 中当前配置的镜像加速源"""
    compose_path = os.path.join(DOCKER_DIR, "docker-compose.yml")
    if not os.path.exists(compose_path):
        return ""
    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("image:"):
                    parts = stripped.split(":", 1)
                    if len(parts) >= 2:
                        img = parts[1].strip().strip("'\"")
                        known_mirrors = [
                            "docker.1ms.run", "docker.xuanyuan.me",
                            "docker.m.daocloud.io", "docker.1panel.live"
                        ]
                        for mirror in known_mirrors:
                            if img.startswith(mirror):
                                return "https://" + mirror
    except Exception:
        pass
    return ""

def probe_mirror(url):
    """测试单个镜像站的 /v2/ OCI 接口响应时间，返回 (url, latency)"""
    import urllib.request
    import urllib.error
    import ssl
    import time
    
    # 针对 OCI 规范测试 /v2/ 接口，更具代表性且支持状态码排查
    probe_url = url.rstrip('/') + '/v2/'
    start_time = time.time()
    try:
        # 伪装为 Docker Client 用户代理，防止部分镜像站防火墙阻断/丢包握手
        req = urllib.request.Request(
            probe_url, 
            method="GET", 
            headers={"User-Agent": "docker/24.0.7"}
        )
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=3.0, context=context) as response:
            pass
        latency = time.time() - start_time
        return url, latency
    except urllib.error.HTTPError as e:
        latency = time.time() - start_time
        # OCI 规范中，未授权的 /v2/ 接口应当返回 401 提示认证，这说明镜像正常存活
        if e.code in (401, 200):
            return url, latency
        else:
            # 403 Forbidden, 429 Too Many Requests 等表示不可用或被限流
            print(f"[Deploy Wizard] 镜像源 {url} 返回不可用状态码: {e.code}")
            return url, 999.0
    except Exception as e:
        print(f"[Deploy Wizard] 镜像源 {url} 连接失败: {e}")
        return url, 999.0

def get_best_mirror():
    """并发测试所有镜像站的延迟，并返回最快的一个"""
    import concurrent.futures
    
    mirrors = [
        "https://docker.m.daocloud.io",
        "https://docker.1ms.run",
        "https://docker.xuanyuan.me",
        "https://docker.1panel.live"
    ]
    best_url = "https://docker.m.daocloud.io"  # 默认备用
    min_latency = 999.0
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(mirrors)) as executor:
            results = executor.map(probe_mirror, mirrors)
            
        for url, latency in results:
            print(f"[Deploy Wizard] 镜像源 {url} 延迟: {latency:.3f} 秒")
            if latency < min_latency:
                min_latency = latency
                best_url = url
    except Exception as e:
        print(f"[Deploy Wizard] 镜像加速源探测异常: {e}")
        
    if min_latency >= 999.0:
        print("[Deploy Wizard] 所有国内镜像源均检测超时，将使用官方直连")
        return ""
        
    print(f"[Deploy Wizard] 检测到最优镜像源: {best_url} (延迟: {min_latency:.3f} 秒)")
    return best_url

def run_cmd_async(cmd, cwd, log_prefix=""):
    """异步执行系统命令并将日志输出到全局队列"""
    log_message(f"执行命令: {cmd} (工作目录: {cwd})")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            cwd=cwd,
            text=True,
            bufsize=1
        )
        while True:
            if deploy_stop_event.is_set():
                process.terminate()
                log_message("部署被强制终止。")
                break
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                log_message(f"{log_prefix}{line.strip()}")
        rc = process.poll()
        log_message(f"命令执行结束，退出码: {rc}")
        return rc == 0
    except Exception as e:
        log_message(f"执行命令失败: {e}")
        return False

def parse_db_url(url, target_db_name):
    """
    解析用户输入的数据库连接，支持 postgresql:// 和 jdbc:postgresql:// 格式
    并转换成指定数据库名称的标准 url 和 jdbc url。
    返回 (standard_url, jdbc_url, db_username, db_password)
    """
    default_host = "localhost"
    default_port = "5432"
    default_user = "postgres"
    default_pass = "iot45722414822"
    
    if not url:
        # 本地默认配置
        std = f"postgresql://{default_user}:{default_pass}@{default_host}:{default_port}/{target_db_name}"
        jdbc = f"jdbc:postgresql://PostgresSQL:5432/{target_db_name}?autoReconnect=true&autoReconnectForPools=true&useUnicode=true&characterEncoding=utf8&createDatabaseIfNotExist=true&allowMultiQueries=true&zeroDateTimeBehavior=convertToNull&stringtype=unspecified"
        return std, jdbc, default_user, default_pass

    # 去除 jdbc: 前缀进行解析
    parse_url = url.strip()
    is_jdbc = parse_url.startswith("jdbc:")
    if is_jdbc:
        parse_url = parse_url[5:]

    user = default_user
    password = default_pass
    host_port = "localhost:5432"
    query_str = ""

    if parse_url.startswith("postgresql://"):
        content = parse_url[13:]
        # 分离 query params
        if "?" in content:
            content, query_str = content.split("?", 1)
        
        # 分离 auth 和 host
        if "@" in content:
            auth, host_part = content.split("@", 1)
            host_port = host_part
            if ":" in auth:
                user, password = auth.split(":", 1)
            else:
                user = auth
        else:
            host_port = content
            
        # 从 host_port 分离 dbname
        if "/" in host_port:
            host_port, _ = host_port.split("/", 1)
    
    # 重新构建 standard url
    std_url = f"postgresql://{user}:{password}@{host_port}/{target_db_name}"
    
    # 重新构建 jdbc url
    jdbc_options = "autoReconnect=true&autoReconnectForPools=true&useUnicode=true&characterEncoding=utf8&createDatabaseIfNotExist=true&allowMultiQueries=true&zeroDateTimeBehavior=convertToNull&stringtype=unspecified"
    if query_str:
        jdbc_url = f"jdbc:postgresql://{host_port}/{target_db_name}?{query_str}"
    else:
        jdbc_url = f"jdbc:postgresql://{host_port}/{target_db_name}?{jdbc_options}"
        
    return std_url, jdbc_url, user, password

def do_deploy_thread(config_data):
    """后台部署线程"""
    global deploy_in_progress, deploy_status
    deploy_status = "running"
    deploy_stop_event.clear()
    
    enabled_middlewares = config_data.get("middlewares", [])
    enabled_services = config_data.get("services", [])
    env_vars = config_data.get("env_vars", {})
    mirror_prefix = config_data.get("mirror_prefix", "")

    # 确保 SRS 配置文件存在以防止容器启动失败
    srs_conf_src = os.path.join(PROJECT_ROOT, ".scripts", "srs", "conf", "docker.conf")
    srs_conf_dest_dir = os.path.join(DOCKER_DIR, "srs_data", "conf")
    srs_conf_dest = os.path.join(srs_conf_dest_dir, "docker.conf")
    if os.path.exists(srs_conf_src):
        os.makedirs(srs_conf_dest_dir, exist_ok=True)
        if not os.path.exists(srs_conf_dest):
            import shutil
            try:
                shutil.copy2(srs_conf_src, srs_conf_dest)
                log_message(f"初始化 SRS 配置文件完成: {srs_conf_dest}")
            except Exception as e:
                log_message(f"[警告] 复制 SRS 配置文件失败: {e}")

    # 自动修复 .sh 脚本的 Windows CRLF 换行符为 Linux LF，防止容器内运行报错
    try:
        sh_converted_count = 0
        for root, dirs, files in os.walk(DOCKER_DIR):
            for file in files:
                if file.endswith(".sh"):
                    filepath = os.path.join(root, file)
                    with open(filepath, "rb") as f:
                        content = f.read()
                    if b"\r\n" in content:
                        content = content.replace(b"\r\n", b"\n")
                        with open(filepath, "wb") as f:
                            f.write(content)
                        sh_converted_count += 1
        if sh_converted_count > 0:
            log_message(f"已自动修复 {sh_converted_count} 个 shell 脚本的换行符格式 (CRLF -> LF)。")
    except Exception as e:
        log_message(f"[警告] 自动修复换行符失败: {e}")

    log_message("============ 开始部署向导流程 ============")
    
    # 1. 动态生成配置
    log_message("第一步：更新 docker-compose.yml 中件配置...")
    if toggle_compose_services(enabled_middlewares, mirror_prefix):
        log_message("中间件配置修剪完成。")
    else:
        log_message("中间件配置更新失败，终止流程。")
        deploy_status = "failed"
        deploy_in_progress = False
        return

    log_message("第二步：配置子服务环境变量 .env 文件...")
    db_mode = env_vars.get("DB_MODE", "local")
    db_url_input = env_vars.get("DATABASE_URL", "")

    # 解析五个主要的数据库 schema
    system_std, system_jdbc, system_user, system_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "ruoyi-vue-pro20")
    device_std, device_jdbc, device_user, device_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "iot-device20")
    message_std, message_jdbc, message_user, message_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "iot-message20")
    video_std, video_jdbc, video_user, video_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "iot-video20")
    gb28181_std, gb28181_jdbc, gb28181_user, gb28181_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "iot-gb2818120")
    ai_std, ai_jdbc, ai_user, ai_pass = parse_db_url(db_url_input if db_mode == "cloud" else "", "iot-ai20")

    use_gpu_str = "True" if env_vars.get("USE_GPU") == "true" else "False"
    kafka_servers = env_vars.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    minio_endpoint = env_vars.get("MINIO_ENDPOINT", "localhost:9000")
    redis_host_input = env_vars.get("REDIS_HOST", "localhost:6379")
    redis_password = env_vars.get("REDIS_PASSWORD", "basiclab@iot975248395")
    
    redis_host = "localhost"
    redis_port = "6379"
    if redis_host_input:
        if ":" in redis_host_input:
            redis_host, redis_port = redis_host_input.split(":", 1)
        else:
            redis_host = redis_host_input

    # 确保 minio_endpoint 有 http/https 协议头
    if minio_endpoint and not minio_endpoint.startswith("http://") and not minio_endpoint.startswith("https://"):
        minio_endpoint_url = f"http://{minio_endpoint}"
    else:
        minio_endpoint_url = minio_endpoint

    llm_vendor = env_vars.get("LLM_VENDOR", "local")
    llm_api_key = env_vars.get("LLM_API_KEY", "")
    llm_base_url = env_vars.get("LLM_BASE_URL", "")
    llm_model_name = env_vars.get("LLM_MODEL_NAME", "")
    
    # 兼容老的 DASHSCOPE_API_KEY 逻辑
    dashscope_key = llm_api_key if llm_vendor == "aliyun" else env_vars.get("DASHSCOPE_API_KEY", "")

    # VIDEO 服务环境变量更新
    video_updates = {
        "DATABASE_URL": video_std,
        "USE_GPU": use_gpu_str,
        "DASHSCOPE_API_KEY": dashscope_key,
        "AI_SERVICE_URL": env_vars.get("AI_SERVICE_URL", "http://localhost:5000"),
        "KAFKA_BOOTSTRAP_SERVERS": kafka_servers,
        "MINIO_ENDPOINT": minio_endpoint,
        "REDIS_HOST": redis_host,
        "REDIS_PORT": redis_port,
        "REDIS_PASSWORD": redis_password,
        "LLM_VENDOR": llm_vendor,
        "LLM_API_KEY": llm_api_key,
        "LLM_BASE_URL": llm_base_url,
        "LLM_MODEL_NAME": llm_model_name
    }

    # AI 服务环境变量更新
    ai_updates = {
        "DATABASE_URL": ai_std,
        "USE_GPU": use_gpu_str,
        "DASHSCOPE_API_KEY": dashscope_key,
        "KAFKA_BOOTSTRAP_SERVERS": kafka_servers,
        "MINIO_ENDPOINT": minio_endpoint,
        "REDIS_HOST": redis_host,
        "REDIS_PORT": redis_port,
        "REDIS_PASSWORD": redis_password,
        "LLM_VENDOR": llm_vendor,
        "LLM_API_KEY": llm_api_key,
        "LLM_BASE_URL": llm_base_url,
        "LLM_MODEL_NAME": llm_model_name
    }

    # DEVICE 服务环境变量更新
    device_updates = {}
    if db_mode == "cloud":
        device_updates.update({
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_SYSTEM_URL": system_jdbc,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_SYSTEM_USERNAME": system_user,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_SYSTEM_PASSWORD": system_pass,
            
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_DEVICE_URL": device_jdbc,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_DEVICE_USERNAME": device_user,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_DEVICE_PASSWORD": device_pass,
            
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MESSAGE_URL": message_jdbc,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MESSAGE_USERNAME": message_user,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_MESSAGE_PASSWORD": message_pass,
            
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_VIDEO_URL": video_jdbc,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_VIDEO_USERNAME": video_user,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_VIDEO_PASSWORD": video_pass,
            
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_GB28181_URL": gb28181_jdbc,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_GB28181_USERNAME": gb28181_user,
            "SPRING_DATASOURCE_DYNAMIC_DATASOURCE_GB28181_PASSWORD": gb28181_pass
        })
        
    # 如果配置了外部 Kafka 或 MinIO 或 Redis，也一并写入 DEVICE/.env
    if kafka_servers and "localhost" not in kafka_servers and "127.0.0.1" not in kafka_servers and "Kafka" not in kafka_servers:
        device_updates["KAFKA_BOOTSTRAP_SERVERS"] = kafka_servers
    if minio_endpoint and "localhost" not in minio_endpoint and "127.0.0.1" not in minio_endpoint and "MinIO" not in minio_endpoint:
        device_updates["MINIO_ENDPOINT"] = minio_endpoint_url
    if redis_host_input and "localhost" not in redis_host_input and "127.0.0.1" not in redis_host_input and "Redis" not in redis_host_input:
        device_updates["SPRING_REDIS_HOST"] = redis_host
        device_updates["SPRING_REDIS_PORT"] = redis_port
        device_updates["SPRING_REDIS_PASSWORD"] = redis_password
        
    # 写入 VIDEO, AI 和 DEVICE 的本地环境变量文件
    video_env_path = os.path.join(PROJECT_ROOT, "VIDEO", ".env")
    ai_env_path = os.path.join(PROJECT_ROOT, "AI", ".env")
    device_env_path = os.path.join(PROJECT_ROOT, "DEVICE", ".env")
    
    update_env_file(video_env_path, video_updates)
    update_env_file(ai_env_path, ai_updates)
    if device_updates:
        update_env_file(device_env_path, device_updates)
        
    log_message("子服务本地环境变量配置更新完成。")

    # 2. 检查 Docker 网络
    log_message("第三步：确保 docker 网络 easyaiot-network 存在...")
    is_windows = sys.platform.startswith("win")
    # 检查网络是否存在
    check_net_cmd = "docker network inspect easyaiot-network"
    try:
        proc = subprocess.run(check_net_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if proc.returncode != 0:
            log_message("创建 easyaiot-network 网络...")
            subprocess.run("docker network create easyaiot-network", shell=True, timeout=10)
        else:
            log_message("网络 easyaiot-network 已存在。")
    except subprocess.TimeoutExpired:
        log_message("[警告] 检测/创建 Docker 网络超时，请确认 Docker Desktop/WSL2 服务是否开启。")

    # 3. 部署中间件
    log_message("第四步：开始拉起基础中间件容器...")
    compose_cmd = "docker compose up -d"
    success = run_cmd_async(compose_cmd, DOCKER_DIR, "[Middleware] ")
    if not success:
        log_message("中间件拉起失败，终止部署。")
        deploy_status = "failed"
        deploy_in_progress = False
        return

    # 4. 部署选中的子服务
    log_message("第五步：拉起子微服务...")
    all_success = True
    for svc in ALL_SERVICES:
        if svc in enabled_services:
            log_message(f"准备拉起微服务: {svc}...")
            svc_dir = os.path.join(PROJECT_ROOT, svc)
            if os.path.exists(svc_dir):
                svc_compose_cmd = "docker compose up -d --build"
                if not run_cmd_async(svc_compose_cmd, svc_dir, f"[{svc}] "):
                    log_message(f"微服务 {svc} 拉起失败。")
                    all_success = False
            else:
                log_message(f"警告：找不到微服务目录 {svc_dir}，跳过部署。")
                all_success = False
        else:
            log_message(f"子服务 {svc} 未勾选，跳过部署。")

    if all_success:
        log_message("============ 部署向导流程执行完毕 ============")
        deploy_status = "success"
    else:
        log_message("============ 部署部分微服务失败 ============")
        deploy_status = "failed"
    deploy_in_progress = False

class DeploymentWizardRequestHandler(http.server.BaseHTTPRequestHandler):
    """Web服务路由处理器"""
    
    def log_message(self, format, *args):
        # 覆写日志，避免控制台刷屏
        pass

    def do_OPTIONS(self):
        """处理CORS预检请求"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/":
            # 首页 HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                html_path = os.path.join(WIZARD_DIR, "templates", "index.html")
                with open(html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"读取页面失败: {e}".encode("utf-8"))

        elif path == "/api/config":
            # 读取当前配置
            enabled_middlewares = parse_compose_services()
            
            # 读取本地.env配置
            video_env_path = os.path.join(PROJECT_ROOT, "VIDEO", ".env")
            video_env = read_env_file(video_env_path)
            redis_h = video_env.get("REDIS_HOST", "localhost")
            redis_p = video_env.get("REDIS_PORT", "6379")
            
            # 识别与回填大模型供应商与配置
            llm_api_key = video_env.get("LLM_API_KEY", video_env.get("DASHSCOPE_API_KEY", ""))
            llm_vendor = video_env.get("LLM_VENDOR", "")
            if not llm_vendor:
                if video_env.get("DASHSCOPE_API_KEY"):
                    llm_vendor = "aliyun"
                else:
                    llm_vendor = "local"
                    
            env_vars = {
                "DATABASE_URL": video_env.get("DATABASE_URL", ""),
                "USE_GPU": "true" if video_env.get("USE_GPU", "False").lower() == "true" else "false",
                "DASHSCOPE_API_KEY": video_env.get("DASHSCOPE_API_KEY", ""),
                "AI_SERVICE_URL": video_env.get("AI_SERVICE_URL", "http://localhost:5000"),
                "KAFKA_BOOTSTRAP_SERVERS": video_env.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
                "MINIO_ENDPOINT": video_env.get("MINIO_ENDPOINT", "localhost:9000"),
                "REDIS_HOST": f"{redis_h}:{redis_p}" if redis_h else "localhost:6379",
                "REDIS_PASSWORD": video_env.get("REDIS_PASSWORD", "basiclab@iot975248395"),
                "LLM_VENDOR": llm_vendor,
                "LLM_API_KEY": llm_api_key,
                "LLM_BASE_URL": video_env.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1" if llm_vendor == "aliyun" else ""),
                "LLM_MODEL_NAME": video_env.get("LLM_MODEL_NAME", "qwen-vl-max" if llm_vendor == "aliyun" else "")
            }
            # 识别外部数据库模式
            if env_vars["DATABASE_URL"] and "localhost" not in env_vars["DATABASE_URL"] and "127.0.0.1" not in env_vars["DATABASE_URL"]:
                env_vars["DB_MODE"] = "cloud"
            else:
                env_vars["DB_MODE"] = "local"

            # 默认启用的微服务
            enabled_services = []
            running_containers = set()
            try:
                res = subprocess.run("docker ps --format \"{{.Names}}\"", shell=True, stdout=subprocess.PIPE, text=True, timeout=3)
                if res.returncode == 0:
                    running_containers = {line.strip().lower() for line in res.stdout.splitlines() if line.strip()}
            except subprocess.TimeoutExpired:
                print("[警告] 检测容器状态超时，可能是 Docker 卡死或未运行。")
            except Exception as e:
                print(f"[警告] 初始化获取运行容器列表失败: {e}")

            for svc in ALL_SERVICES:
                svc_compose = os.path.join(PROJECT_ROOT, svc, "docker-compose.yml")
                if not os.path.exists(svc_compose):
                    svc_compose = os.path.join(PROJECT_ROOT, svc, "docker-compose.yaml")
                if os.path.exists(svc_compose):
                    # 判断子容器是否正在运行
                    is_running = any(svc.lower() in name for name in running_containers)
                    if is_running:
                        enabled_services.append(svc)

            current_mirror = detect_current_mirror()
            if not current_mirror:
                print("[Deploy Wizard] 未检测到本地配置的镜像源，开始自动探测最优镜像站...")
                current_mirror = get_best_mirror()

            response = {
                "middlewares": [m for m, active in enabled_middlewares.items() if active],
                "services": enabled_services,
                "env_vars": env_vars,
                "docker_mirror": current_mirror,
                "local_ip": get_ip_address()
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        elif path == "/api/probe_mirrors":
            import concurrent.futures
            # 需要测试的镜像站点列表 (前端对应的真实 value, 显示名称, 测速 URL)
            mirrors_config = [
                {"value": "https://docker.m.daocloud.io", "name": "DaoCloud 镜像", "url": "https://docker.m.daocloud.io"},
                {"value": "https://docker.1ms.run", "name": "毫秒镜像", "url": "https://docker.1ms.run"},
                {"value": "https://docker.xuanyuan.me", "name": "轩辕镜像", "url": "https://docker.xuanyuan.me"},
                {"value": "https://docker.1panel.live", "name": "1Panel 镜像", "url": "https://docker.1panel.live"},
                {"value": "", "name": "直连 (Docker Hub 官方默认)", "url": "https://registry-1.docker.io"}
            ]
            
            results = []
            best_val = ""
            min_latency = 999.0
            
            urls_to_probe = [item["url"] for item in mirrors_config]
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls_to_probe)) as executor:
                    probe_results = list(executor.map(probe_mirror, urls_to_probe))
                
                latency_map = {url: lat for url, lat in probe_results}
                
                for item in mirrors_config:
                    lat = latency_map.get(item["url"], 999.0)
                    results.append({
                        "value": item["value"],
                        "name": item["name"],
                        "latency": round(lat, 3)
                    })
                    if lat < min_latency:
                        min_latency = lat
                        best_val = item["value"]
            except Exception as e:
                print(f"[Deploy Wizard] 接口并发测速异常: {e}")
                
            response = {
                "status": "success",
                "best_mirror": best_val,
                "mirrors": results
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        elif path == "/api/status":
            # 获取容器状态
            containers_status = []
            try:
                # 获取正在运行的容器，设置 3 秒超时限制
                res = subprocess.run("docker ps --format \"{{.Names}}|{{.Status}}|{{.Image}}\"", shell=True, stdout=subprocess.PIPE, text=True, timeout=3)
                for line in res.stdout.split("\n"):
                    if line.strip():
                        parts = line.split("|")
                        containers_status.append({
                            "name": parts[0],
                            "status": parts[1],
                            "image": parts[2],
                            "running": True
                        })
                # 获取已停止的容器，设置 3 秒超时限制
                res_all = subprocess.run("docker ps -a --format \"{{.Names}}|{{.Status}}|{{.Image}}\"", shell=True, stdout=subprocess.PIPE, text=True, timeout=3)
                running_names = {c["name"] for c in containers_status}
                for line in res_all.stdout.split("\n"):
                    if line.strip():
                        parts = line.split("|")
                        if parts[0] not in running_names:
                            containers_status.append({
                                "name": parts[0],
                                "status": parts[1],
                                "image": parts[2],
                                "running": False
                            })
            except subprocess.TimeoutExpired:
                containers_status = [{"error": "获取 Docker 状态超时！请确认您的 Docker Desktop / WSL2 引擎是否正常运行。"}]
            except Exception as e:
                containers_status = [{"error": f"获取 Docker 状态失败: {e}"}]

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(containers_status).encode("utf-8"))

        elif path == "/api/logs":
            # Server-Sent Events (SSE) 实时日志流
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            last_index = 0
            try:
                while True:
                    # 如果没有更多日志且部署结束，可以适当跳出或维持心跳
                    if last_index < len(deploy_logs):
                        for i in range(last_index, len(deploy_logs)):
                            log_line = deploy_logs[i]
                            self.wfile.write(f"data: {log_line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_index = len(deploy_logs)
                    else:
                        # 发送心跳数据以检测连接是否中断
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    
                    if not deploy_in_progress and last_index >= len(deploy_logs):
                        # 部署已结束，发送带状态的结束标示并断开
                        status_suffix = "SUCCESS" if deploy_status == "success" else "FAILED"
                        self.wfile.write(f"data: [EOF:{status_suffix}]\n\n".encode("utf-8"))
                        self.wfile.flush()
                        break
                    
                    time.sleep(0.5)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.error) as e:
                print(f"[Deploy Wizard] SSE 客户端连接断开: {e}")
            except Exception as e:
                print(f"[Deploy Wizard] SSE 异常: {e}")

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self):
        global deploy_in_progress, deploy_thread, last_ui_log_time
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode("utf-8"))

        if path == "/api/deploy":
            if deploy_in_progress:
                response = {"status": "error", "message": "部署已在运行中，请勿重复提交"}
            else:
                deploy_in_progress = True
                deploy_logs.clear()
                last_ui_log_time = time.time()
                deploy_logs.append("[Deploy Wizard] 初始化部署环境成功，准备开始任务...")
                
                # 开启线程运行部署
                deploy_thread = threading.Thread(target=do_deploy_thread, args=(data,))
                deploy_thread.daemon = True
                deploy_thread.start()
                response = {"status": "success", "message": "部署任务已启动"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        elif path == "/api/stop":
            # 停止并清理部署服务
            deploy_stop_event.set()
            deploy_logs.clear()
            last_ui_log_time = time.time()
            deploy_logs.append("[Deploy Wizard] 收到终止部署请求，正在清理容器...")
            
            def stop_work():
                # 停止中间件
                run_cmd_async("docker compose down", DOCKER_DIR, "[Stop] ")
                # 停止核心模块
                for svc in ALL_SERVICES:
                    svc_dir = os.path.join(PROJECT_ROOT, svc)
                    if os.path.exists(svc_dir):
                        run_cmd_async("docker compose down", svc_dir, f"[Stop {svc}] ")
                log_message("[Deploy Wizard] 所有 Docker 容器已安全停止并卸载。")
            
            threading.Thread(target=stop_work).start()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "正在卸载服务"}).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

def start_wizard():
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, DeploymentWizardRequestHandler)
    
    # 打印欢迎语并提示
    ip = get_ip_address()
    print("=" * 60)
    print("      * EasyAIoT 一键部署配置向导启动成功！ *")
    print(f"      本地访问地址: http://localhost:{PORT}")
    print(f"      局域网访问地址: http://{ip}:{PORT}")
    print("      请保持此命令行窗口打开，可在页面点点点进行部署")
    print("=" * 60)
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭向导服务...")
        httpd.server_close()
        sys.exit(0)

if __name__ == "__main__":
    start_wizard()

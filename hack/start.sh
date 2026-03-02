#!/bin/bash

# -------------------------------
# 基本变量定义
# -------------------------------
CONTAINER_NAME="evenly-backend-service"
IMAGE_NAME="crpi-3xux8vqn35fw00z9.cn-shanghai.personal.cr.aliyuncs.com/project_hub/${CONTAINER_NAME}"
IMAGE_TAG="1.0.0"
FULL_IMAGE_NAME="${IMAGE_NAME}:${IMAGE_TAG}"

# 主机配置目录
HOST_ETC_DIR="/opt/evenly-backend-service"
HOST_CONFIG_FILE="${HOST_ETC_DIR}/config/config.yaml"

# 容器路径
CONTAINER_CONFIG_PATH="/app/config/config.yaml"

# 端口配置
HOST_PORT=8000
CONTAINER_PORT=3000

# -------------------------------
# 彩色输出函数
# -------------------------------
print_info()  { echo -e "\033[1;32m[INFO]\033[0m  $1"; }
print_warn()  { echo -e "\033[1;33m[WARN]\033[0m  $1"; }
print_error() { echo -e "\033[1;31m[ERROR]\033[0m $1"; }

# -------------------------------
# 检查环境
# -------------------------------
check_env() {
    if [ ! -f "${HOST_CONFIG_FILE}" ]; then
        print_error "配置文件不存在: ${HOST_CONFIG_FILE}"
        exit 1
    fi

}

# -------------------------------
# 镜像检查与拉取
# -------------------------------
ensure_image() {
    if ! docker image inspect "${FULL_IMAGE_NAME}" >/dev/null 2>&1; then
        print_warn "镜像 ${FULL_IMAGE_NAME} 不存在，开始拉取..."
        docker pull "${FULL_IMAGE_NAME}"
        # shellcheck disable=SC2181
        if [ $? -ne 0 ]; then
            print_error "镜像拉取失败！"
            exit 1
        fi
    else
        print_info "镜像 ${FULL_IMAGE_NAME} 已存在。"
    fi
}

# -------------------------------
# 容器状态判断
# -------------------------------
container_exists() {
    docker ps -a --format '{{.Names}}' | grep -wq "${CONTAINER_NAME}"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -wq "${CONTAINER_NAME}"
}

# -------------------------------
# 启动容器
# -------------------------------
start_container() {
    print_info "准备启动 Webplus OpenAPI Server..."

    # 检查端口是否被占用
    if lsof -i :${HOST_PORT} >/dev/null 2>&1; then
        print_error "端口 ${HOST_PORT} 已被占用，请先释放或修改端口号。"
        exit 1
    fi

    # 若旧容器存在则删除
    if container_exists; then
        print_warn "检测到旧容器，正在删除..."
        docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1
    fi

    ensure_image

    docker run -d \
        --name "${CONTAINER_NAME}" \
        --network host \
        -v "${HOST_CONFIG_FILE}:${CONTAINER_CONFIG_PATH}" \
        "${FULL_IMAGE_NAME}"

    # shellcheck disable=SC2181
    if [ $? -eq 0 ]; then
        print_info "容器启动成功！"
        print_info "访问地址: http://$(hostname -I | awk '{print $1}'):${HOST_PORT}"
    else
        print_error "容器启动失败，请检查配置或日志。"
        exit 1
    fi
}

# -------------------------------
# 停止容器
# -------------------------------
stop_container() {
    if container_running; then
        docker stop "${CONTAINER_NAME}"
        print_info "容器 ${CONTAINER_NAME} 已停止。"
    else
        print_warn "容器未运行。"
    fi
}

# -------------------------------
# 重启容器
# -------------------------------
restart_container() {
    stop_container
    sleep 2
    start_container
}

# -------------------------------
# 查看状态
# -------------------------------
show_status() {
    if container_running; then
        print_info "容器 ${CONTAINER_NAME} 正在运行。"
        docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    elif container_exists; then
        print_warn "容器 ${CONTAINER_NAME} 已存在但未运行。"
        docker ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}"
    else
        print_warn "容器 ${CONTAINER_NAME} 不存在。"
    fi
}

# -------------------------------
# 查看日志
# -------------------------------
show_logs() {
    if container_exists; then
        docker logs -f "${CONTAINER_NAME}"
    else
        print_warn "容器不存在，请先启动。"
    fi
}

# -------------------------------
# 更新镜像并重启
# -------------------------------
update_container() {
    print_info "开始更新镜像..."
    docker pull "${FULL_IMAGE_NAME}"
    restart_container
}

# -------------------------------
# 帮助信息
# -------------------------------
show_help() {
    echo "======================================================="
    echo " Webplus Server 管理脚本"
    echo "======================================================="
    echo "用法:"
    echo "  $0 start       启动服务"
    echo "  $0 stop        停止服务"
    echo "  $0 restart     重启服务"
    echo "  $0 status      查看状态"
    echo "  $0 logs        查看日志"
    echo "  $0 update      拉取最新镜像并重启"
    echo "  $0 debug       调试模式（前台运行）"
    echo "  $0 help        显示帮助信息"
    echo
    echo "配置挂载:"
    echo "  ${HOST_CONFIG_FILE}  →  /app/evenly-backend-service/etc/config.yaml"
    echo "访问端口: http://<主机IP>:${HOST_PORT}"
    echo "======================================================="
}

# -------------------------------
# 主逻辑
# -------------------------------
case "$1" in
    start)
        check_env
        start_container
        ;;
    stop)
        stop_container
        ;;
    restart)
        restart_container
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    update)
        update_container
        ;;
    debug)
        check_env
        debug_container
        ;;
    help|*)
        show_help
        ;;
esac

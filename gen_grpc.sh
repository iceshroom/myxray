#!/bin/bash
# 设置 proto 文件所在的根目录（默认为当前目录 '.'，可根据需要修改）
ROOT_DIR="."

# 检查 grpcio-tools 是否已安装
if ! python -c "import grpc_tools" &> /dev/null; then
    echo "错误：grpcio-tools 未安装。请执行：pip install grpcio-tools"
    exit 1
fi

# 查找所有 .proto 文件（递归）
PROTO_FILES=$(find "$ROOT_DIR" -name "*.proto" -type f)

if [ -z "$PROTO_FILES" ]; then
    echo "在 $ROOT_DIR 下未找到任何 .proto 文件"
    exit 0
fi

# 一次性调用 protoc，将所有文件路径传给它
# -I 设为根目录，确保 import 路径正确
# --python_out, --grpc_python_out, --pyi_out 都设为根目录，使生成文件与源文件同目录
uv run python -m grpc_tools.protoc -I="$ROOT_DIR" \
    --python_out="$ROOT_DIR" \
    --grpc_python_out="$ROOT_DIR" \
    --pyi_out="$ROOT_DIR" \
    $PROTO_FILES

if [ $? -eq 0 ]; then
    echo "✅ 已成功为所有 .proto 文件生成 Python gRPC 代码。"
else
    echo "❌ 生成过程出现错误，请检查上述输出。"
fi